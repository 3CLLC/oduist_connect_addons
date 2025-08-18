# -*- coding: utf-8 -*

import json
import hmac
from hashlib import sha256
import logging
import requests
import time
from datetime import timedelta
from odoo import http, SUPERUSER_ID, registry, release
from werkzeug.exceptions import BadRequest, NotFound

from odoo.exceptions import UserError

logger = logging.getLogger(__name__)


class ConnectPlusController(http.Controller):

    @http.route('/connect/transcript/<int:rec_id>', methods=['POST'], type='json',
                auth='public', csrf=False)
    def upload_transcript(self, rec_id):
        # Public method protected by the one-time transcription token.
        data = json.loads(http.request.httprequest.get_data(as_text=True))
        rec = http.request.env['connect.recording'].sudo().search([
            ('id', '=', rec_id), ('transcription_token', '!=', False),
            ('transcription_token', '=', data['transcription_token'])
        ])
        if not rec:
            logger.warning('Transcription token %s not found for recording %s',
                data['transcription_token'], rec_id)
            raise NotFound()
        rec.with_user(SUPERUSER_ID).update_transcript(data)
        logger.info('Transcript for recording %s saved.', rec_id)
        return True

    @http.route('/connect/recording/<int:record_id>', type='http', auth='user')
    def serve_recording(self, record_id):
        # Access the recording as logged in user.
        recording = http.request.env['connect.recording'].browse(record_id)
        if not recording.exists() or not recording.media_url:
            return http.Response(status=404)
        return self._serve_media(recording.media_url)

    @http.route('/connect/voicemail/<int:record_id>', type='http', auth='user')
    def serve_voicemail(self, record_id):
        # Access the recording as logged in user.
        call = http.request.env['connect.call'].browse(record_id)
        if not call.exists() or not call.voicemail_url:
            return http.Response(status=404)
        return self._serve_media(call.voicemail_url)

    def _serve_media(self, media_url):
        media_name = '{}.wav'.format(media_url.split('/')[-1])
        account_sid = http.request.env['connect.settings'].sudo().get_param('account_sid')
        auth_token = http.request.env['connect.settings'].sudo().get_param('auth_token')
        response = requests.get(media_url, auth=(account_sid, auth_token))
        if response.status_code == 200:
            # Create the response
            res = http.Response(response.content, content_type='audio/wav')
            res.headers['Content-Disposition'] = http.content_disposition(media_name)
            return res
        else:
            raise UserError("Failed to download the media. Status code: %s" % response.status_code)

    @http.route('/connect/<string:extension_number>', methods=['GET', 'POST'], type='http', auth='public', csrf=False)
    def extension_handler(self, extension_number, **kw):
        """Handle extension calls via direct URL"""
        logger.info(f'Extension handler called for extension {extension_number}')
        logger.info(f'Parameters: {kw}')
        
        # Find the extension
        exten = http.request.env['connect.exten'].sudo().search([('number', '=', extension_number)])
        if not exten:
            return '<Response><Say>Extension not found. Goodbye!</Say></Response>'
        
        # Render the extension with the webhook parameters
        return exten.render(request=kw, params=kw)
    
    @http.route('/connect/dial_complete', methods=['GET', 'POST'], type='http', auth='public', csrf=False)
    def dial_complete_handler(self, **kw):
        """Handle Dial action completion for transfer redirects and update call completion fields"""
        from twilio.twiml.voice_response import VoiceResponse
        
        dial_status = kw.get('DialCallStatus')
        dial_call_sid = kw.get('DialCallSid')  # SID of the transfer recipient call
        original_call_sid = kw.get('CallSid')  # SID of the redirect call
        
        logger.info(f'=== DIAL COMPLETE HANDLER ===')
        logger.info(f'DialCallStatus: {dial_status}')
        logger.info(f'DialCallSid: {dial_call_sid}')
        logger.info(f'CallSid: {original_call_sid}')
        logger.info(f'All params: {kw}')
        
        # Process transfer completion to update original call fields
        try:
            self._process_extension_redirect_completion(kw)
        except Exception as e:
            logger.error(f'Failed to process transfer completion: {e}', exc_info=True)
        
        response = VoiceResponse()
        
        if dial_status == 'completed':
            # Call was answered successfully - hang up the redirect call
            logger.info('Transfer answered - hanging up redirect call')
            response.hangup()
        else:
            # Call was not answered - provide personalized voicemail
            logger.info(f'Transfer not answered (status: {dial_status}) - providing personalized voicemail')
            
            # Try to find the target user for personalized voicemail
            # Reuse the same logic from completion processing
            try:
                original_call = self._find_original_call_for_redirect_completion(original_call_sid, dial_call_sid)
                if original_call:
                    # Use the same comprehensive fallback logic as completion processing
                    transfer_recipient = None
                    
                    # First try the original call SID (redirect call)
                    if original_call_sid:
                        transfer_recipient = original_call.get_transfer_target(original_call_sid)
                    
                    # Fallback: try with dial_call_sid (transfer recipient call)  
                    if not transfer_recipient and dial_call_sid:
                        transfer_recipient = original_call.get_transfer_target(dial_call_sid)
                        
                    # Fallback: check ParentCallSid from webhook params  
                    if not transfer_recipient:
                        parent_call_sid = kw.get('ParentCallSid')
                        if parent_call_sid:
                            transfer_recipient = original_call.get_transfer_target(parent_call_sid)
                    
                    # FINAL FALLBACK: Use most recent transferred user
                    if not transfer_recipient and original_call.transferred_users:
                        transfer_recipient = original_call.transferred_users[-1]  # Most recent transfer
                        logger.info(f'Using fallback for voicemail: most recent transferred user {transfer_recipient.login}')
                    
                    if transfer_recipient:
                        # Get the PBX user for voicemail prompt
                        pbx_user = http.request.env['connect.user'].sudo().search([
                            ('user', '=', transfer_recipient.id)
                        ], limit=1)
                        
                        if pbx_user and pbx_user.voicemail_enabled and pbx_user.voicemail_prompt:
                            # Use personalized voicemail prompt
                            logger.info(f'Using personalized voicemail for {transfer_recipient.login}')
                            personalized_prompt = pbx_user.render_voicemail_prompt()
                            response.say(personalized_prompt)
                        else:
                            # Fallback to generic message
                            logger.info(f'Using generic voicemail (user has no personalized prompt)')
                            response.say('Please leave a message after the tone.')
                    else:
                        logger.warning(f'Could not find transfer recipient for personalized voicemail')
                        response.say('Please leave a message after the tone.')
                else:
                    logger.warning(f'Could not find original call for personalized voicemail')
                    response.say('Please leave a message after the tone.')
            except Exception as e:
                logger.error(f'Error setting up personalized voicemail: {e}')
                response.say('Please leave a message after the tone.')
                
            response.record(maxLength=120, finishOnKey='#', playBeep=True)
        
        return response.to_xml()
    
    def _process_extension_redirect_completion(self, webhook_params):
        """
        Process completion of extension redirect transfers.
        Updates the original call's completion fields based on transfer outcome.
        """
        dial_call_status = webhook_params.get('DialCallStatus')
        dial_call_sid = webhook_params.get('DialCallSid')
        original_call_sid = webhook_params.get('CallSid')
        
        logger.info(f'=== PROCESSING EXTENSION REDIRECT COMPLETION ===')
        logger.info(f'Original CallSid: {original_call_sid}, DialCallSid: {dial_call_sid}, Status: {dial_call_status}')
        
        # Find original call using transfer context or recent transfers
        original_call = self._find_original_call_for_redirect_completion(original_call_sid, dial_call_sid)
        if not original_call:
            logger.warning(f'Could not find original call for redirect completion')
            return
            
        logger.info(f'Found original call {original_call.id} for transfer completion processing')
        
        # Find transfer recipient user from transfer context
        # Try multiple SID patterns from the webhook
        transfer_recipient = None
        
        # First try the original call SID (redirect call)
        if original_call_sid:
            transfer_recipient = original_call.get_transfer_target(original_call_sid)
        
        # Fallback: try with dial_call_sid (transfer recipient call)  
        if not transfer_recipient and dial_call_sid:
            transfer_recipient = original_call.get_transfer_target(dial_call_sid)
            
        # Fallback: check ParentCallSid from webhook params  
        if not transfer_recipient:
            parent_call_sid = webhook_params.get('ParentCallSid')
            if parent_call_sid:
                transfer_recipient = original_call.get_transfer_target(parent_call_sid)
                logger.info(f'Trying ParentCallSid {parent_call_sid} for transfer recipient')
        
        # FINAL FALLBACK: If still no recipient found, use the most recent transferred user
        # This handles cases where transfer context lookup fails but we know transfers occurred
        if not transfer_recipient and original_call.transferred_users:
            transfer_recipient = original_call.transferred_users[-1]  # Most recent transfer
            logger.info(f'Using fallback: most recent transferred user {transfer_recipient.login}')
        
        if not transfer_recipient:
            logger.warning(f'Could not find transfer recipient for completion processing - no transferred_users found')
            return
            
        logger.info(f'Transfer recipient: {transfer_recipient.login}')
        
        # Update completion fields based on transfer outcome
        if dial_call_status == 'completed':
            # Transfer successful - recipient answered
            logger.info(f'Transfer completed successfully - setting completed_by_user to {transfer_recipient.login}')
            original_call.completed_by_user = transfer_recipient
            
            # Create/update a channel record for the transfer recipient to ensure proper field population
            self._create_or_update_transfer_channel(original_call, dial_call_sid, transfer_recipient, 'completed', webhook_params)
            
            # CRITICAL: For completed transfers, terminate external call legs to prevent VM fall-through
            self._terminate_external_call_after_transfer_completion(original_call, dial_call_sid, transfer_recipient)
            
        else:
            # Transfer failed - recipient didn't answer
            logger.info(f'Transfer failed (status: {dial_call_status}) - leaving completed_by_user empty for missed call notification')
            # Don't set completed_by_user - this will trigger missed call notifications
            
            # Create/update a channel record for the failed transfer
            self._create_or_update_transfer_channel(original_call, dial_call_sid, transfer_recipient, dial_call_status, webhook_params)
        
        logger.info(f'=== EXTENSION REDIRECT COMPLETION PROCESSING COMPLETE ===')
    
    def _find_original_call_for_redirect_completion(self, original_call_sid, dial_call_sid):
        """Find the original call that initiated this transfer redirect"""
        # Strategy 1: Look for calls with transfer context containing either SID
        recent_calls = http.request.env['connect.call'].sudo().search([
            ('transfer_context', '!=', False),
            ('create_date', '>=', http.request.env.context.get('tz_offset_timestamp', 
                http.request.env['connect.call'].sudo().search([], order='id desc', limit=1).create_date - timedelta(minutes=5)))
        ])
        
        for call in recent_calls:
            if call.transfer_context:
                # Check if either SID is in the transfer context (handle None values)
                context_str = str(call.transfer_context)
                if ((original_call_sid and original_call_sid in context_str) or 
                    (dial_call_sid and dial_call_sid in context_str)):
                    logger.info(f'Found original call {call.id} via transfer context')
                    return call
        
        # Strategy 2: Look for recent calls with transferred_users
        recent_transfers = http.request.env['connect.call'].sudo().search([
            ('transferred_users', '!=', False),
            ('create_date', '>=', http.request.env.context.get('tz_offset_timestamp', 
                http.request.env['connect.call'].sudo().search([], order='id desc', limit=1).create_date - timedelta(minutes=5))),
            ('status', 'not in', ['completed', 'failed', 'busy', 'no-answer'])
        ], limit=5)
        
        if recent_transfers:
            logger.info(f'Found {len(recent_transfers)} recent transfer calls - using most recent')
            return recent_transfers[0]
            
        return None
    
    def _create_or_update_transfer_channel(self, call, dial_call_sid, transfer_recipient, status, webhook_params):
        """Create or update a channel record for the transfer recipient to ensure proper field population"""
        try:
            # Check if channel already exists
            existing_channel = http.request.env['connect.channel'].sudo().search([
                ('sid', '=', dial_call_sid),
                ('call', '=', call.id)
            ], limit=1)
            
            if existing_channel:
                # Update existing channel
                logger.info(f'Updating existing transfer channel {existing_channel.id}')
                existing_channel.write({
                    'status': status,
                    'duration': int(webhook_params.get('DialCallDuration', 0))
                })
                return existing_channel
            else:
                # Create new transfer channel
                logger.info(f'Creating new transfer channel for {transfer_recipient.login}')
                
                # Find parent channel
                parent_channel = call.channels.filtered(lambda c: not c.parent_channel)
                if not parent_channel:
                    logger.warning(f'No parent channel found for call {call.id}')
                    return None
                parent_channel = parent_channel[0]
                
                # Find PBX user for transfer recipient
                pbx_user = http.request.env['connect.user'].sudo().search([
                    ('user', '=', transfer_recipient.id)
                ], limit=1)
                
                if not pbx_user:
                    logger.warning(f'No PBX user found for {transfer_recipient.login}')
                    return None
                
                channel_data = {
                    'sid': dial_call_sid,
                    'call': call.id,
                    'parent_channel': parent_channel.id,
                    'technical_direction': 'outbound-dial',
                    'status': status,
                    'duration': int(webhook_params.get('DialCallDuration', 0)),
                    'called_pbx_user': pbx_user.id,
                    'called_user': transfer_recipient.id,
                    'call_source': 'transfer',
                    'caller': parent_channel.caller,
                    'called': pbx_user.uri
                }
                
                new_channel = http.request.env['connect.channel'].sudo().create(channel_data)
                logger.info(f'Created transfer channel {new_channel.id} for {transfer_recipient.login}')
                return new_channel
                
        except Exception as e:
            logger.error(f'Failed to create/update transfer channel: {e}', exc_info=True)
            return None
    
    def _terminate_external_call_after_transfer_completion(self, call, transfer_recipient_sid, transfer_recipient):
        """
        Terminate external call legs after successful transfer completion to prevent voicemail fall-through.
        This addresses the issue where external callers go to voicemail when internal users hang up completed calls.
        """
        try:
            logger.info(f'=== TERMINATING EXTERNAL CALLS AFTER TRANSFER COMPLETION ===')
            logger.info(f'Call: {call.id}, Transfer recipient: {transfer_recipient.login}')
            
            # For outgoing calls, find and terminate the external call leg
            if call.direction == 'outgoing':
                external_call_sid = call.get_external_call_leg()
                if external_call_sid:
                    logger.info(f'Found external call leg: {external_call_sid}')
                    
                    # Get Twilio client
                    client = http.request.env['connect.settings'].sudo().get_client()
                    
                    # Check if external call is still active
                    try:
                        external_call = client.calls(external_call_sid).fetch()
                        if external_call.status in ['in-progress', 'ringing']:
                            # External call is still active - set up termination logic
                            # Instead of immediate termination, we'll modify the call to hang up when transfer recipient hangs up
                            logger.info(f'External call {external_call_sid} is active - will terminate when transfer recipient hangs up')
                            
                            # Store termination context for later processing
                            self._store_external_call_termination_context(call, external_call_sid, transfer_recipient_sid)
                        else:
                            logger.info(f'External call {external_call_sid} already ended ({external_call.status})')
                    except Exception as e:
                        logger.warning(f'Could not check external call status: {e}')
                else:
                    logger.warning(f'No external call leg found for outgoing call {call.id}')
                    
            # For incoming calls, the external caller is the original caller
            else:
                # Find the original external caller channel
                external_channels = call.channels.filtered(lambda c: not c.parent_channel and not c.caller_pbx_user)
                if external_channels:
                    external_channel = external_channels[0]
                    logger.info(f'Found external caller channel: {external_channel.sid}')
                    
                    # Store termination context for later processing  
                    self._store_external_call_termination_context(call, external_channel.sid, transfer_recipient_sid)
                else:
                    logger.info(f'No external caller channel found for incoming call {call.id}')
            
            logger.info(f'=== EXTERNAL CALL TERMINATION SETUP COMPLETE ===')
            
        except Exception as e:
            logger.error(f'Failed to set up external call termination: {e}', exc_info=True)
    
    def _store_external_call_termination_context(self, call, external_call_sid, transfer_recipient_sid):
        """Store context for terminating external calls when transfer recipients hang up"""
        try:
            current_context = call.transfer_context or {}
            current_context['_external_termination'] = {
                'external_call_sid': external_call_sid,
                'transfer_recipient_sid': transfer_recipient_sid,
                'setup_time': http.request.env.cr.now()
            }
            call.transfer_context = current_context
            logger.info(f'Stored external termination context: {external_call_sid} -> {transfer_recipient_sid}')
        except Exception as e:
            logger.error(f'Failed to store external termination context: {e}')

    @http.route('/connect/health/<string:uid>/', methods=['GET', 'POST'], type='http', auth='public', csrf=False)
    def health_check(self, uid):
        instance_uid = http.request.env['connect.settings'].sudo().get_param('instance_uid')
        if uid == instance_uid:
            return "True"
        else:
            return "False"
