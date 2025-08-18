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
            # Call was not answered - allow voicemail
            logger.info(f'Transfer not answered (status: {dial_status}) - allowing voicemail')
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
        transfer_recipient = original_call.get_transfer_target(original_call_sid)
        if not transfer_recipient:
            # Fallback: try with dial_call_sid
            transfer_recipient = original_call.get_transfer_target(dial_call_sid)
        
        if not transfer_recipient:
            logger.warning(f'Could not find transfer recipient for completion processing')
            return
            
        logger.info(f'Transfer recipient: {transfer_recipient.login}')
        
        # Update completion fields based on transfer outcome
        if dial_call_status == 'completed':
            # Transfer successful - recipient answered
            logger.info(f'Transfer completed successfully - setting completed_by_user to {transfer_recipient.login}')
            original_call.completed_by_user = transfer_recipient
            
            # Create/update a channel record for the transfer recipient to ensure proper field population
            self._create_or_update_transfer_channel(original_call, dial_call_sid, transfer_recipient, 'completed', webhook_params)
            
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
                # Check if either SID is in the transfer context
                if (original_call_sid in str(call.transfer_context) or 
                    dial_call_sid in str(call.transfer_context)):
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

    @http.route('/connect/health/<string:uid>/', methods=['GET', 'POST'], type='http', auth='public', csrf=False)
    def health_check(self, uid):
        instance_uid = http.request.env['connect.settings'].sudo().get_param('instance_uid')
        if uid == instance_uid:
            return "True"
        else:
            return "False"
