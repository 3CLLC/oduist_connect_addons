# -*- coding: utf-8 -*-

import json
import logging
import re
from urllib.parse import urljoin
import uuid
from odoo import fields, models, api, release, SUPERUSER_ID, tools
from odoo.exceptions import ValidationError
from twilio.twiml.voice_response import VoiceResponse, Say, Dial, Conference, Client, Number, Sip
from .settings import debug

logger = logging.getLogger(__name__)

CALL_END_STATUSES = ['completed', 'busy', 'failed', 'no-answer', 'canceled']

IGNORE_ERROR_CODES = ['32009']


class Call(models.Model):
    _name = 'connect.call'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = 'Call'
    _order = 'id desc'

    name = fields.Char(compute='_get_name')
    channels = fields.One2many('connect.channel', 'call', readonly=True)
    recording = fields.Many2one('connect.recording', compute='_get_recording_data')
    transcript = fields.Text(compute='_get_recording_data')
    if release.version_info[0] >= 17.0:
        recording_widget = fields.Html(compute='_get_recording_data', sanitize=False)
    else:
        recording_widget = fields.Char(compute='_get_recording_data')
    recording_icon = fields.Html(compute='_get_recording_data', string='R')
    summary = fields.Html()
    called = fields.Char(readonly=True)
    caller = fields.Char(readonly=True)
    parent_call = fields.Many2one('connect.call', ondelete='cascade', readonly=True)
    partner = fields.Many2one('res.partner', ondelete='set null')
    partner_img = fields.Binary(related='partner.image_1920', string='Partner Image')
    direction = fields.Char(index=True, readonly=True)
    status = fields.Char(readonly=True)
    duration = fields.Integer(string='Seconds', readonly=True)
    duration_minutes = fields.Float(string='Minutes', compute='_get_duration_human', store=True)
    duration_human = fields.Char(compute='_get_duration_human', string='Duration', store=True)
    # PBX users are Connect SIP or Client users.
    caller_pbx_user = fields.Many2one('connect.user', ondelete='set null', string='Caller PBX User', readonly=True)
    answered_pbx_user = fields.Many2one('connect.user', ondelete='set null', string='Answered PBX User', readonly=True)
    called_pbx_users = fields.Many2many('connect.user', readonly=True)
    # Users are Odoo accounts.
    caller_user = fields.Many2one('res.users', string='Caller User', ondelete='set null', readonly=True)
    caller_user_img = fields.Binary(related='caller_user.image_1920')
    called_users = fields.Many2many('res.users', readonly=True)
    answered_user = fields.Many2one('res.users', ondelete='set null', string='Answered User', readonly=True)
    answered_user_img = fields.Binary(related='answered_user.image_1920', string='Answered User Avatar')
    # Transfer tracking fields
    transferred_users = fields.Many2many('res.users', 'connect_call_transfer_rel', 'call_id', 'user_id', string='Transferred Users', readonly=True)
    completed_by_user = fields.Many2one('res.users', ondelete='set null', string='Completed By', readonly=True)
    # Call pattern tracking
    call_pattern = fields.Selection([
        ('ring_group', 'Ring Group (Multiple Users)'),
        ('direct_call', 'Direct Call (Single User)')
    ], string='Call Pattern', readonly=True, help='Detected call pattern: ring group (press 0) vs direct call (press 1 for extension)')
    # Scheduled fields.
    scheduled_datetime = fields.Datetime()
    # Voicemail fields
    voicemail_url = fields.Char(readonly=True)
    voicemail_duration = fields.Integer(readonly=True)
    voicemail_icon = fields.Html(compute='_get_voicemail_icon', string='V', store=True)
    if release.version_info[0] >= 17.0:
        voicemail_widget = fields.Html(compute='_get_voicemail_widget', string='VoiceMail', sanitize=False)
    else:
        voicemail_widget = fields.Char(compute='_get_voicemail_widget', string='VoiceMail')
    # Reference, to submit call history and summary.
    ref = fields.Reference(selection=[('res.partner', 'Partner')], compute='_get_ref')
    has_error = fields.Boolean(index=True)
    error_code = fields.Char(readonly=True)
    error_message = fields.Text(readonly=True)

    def _get_name(self):
        for rec in self:
            try:
                started = fields.Datetime.context_timestamp(rec, rec.create_date)
                formatted_time = fields.Datetime.to_string(started)
                rec.name = '{} {} call at {}'.format(rec.status, rec.direction, formatted_time).capitalize()
            except Exception:
                logger.exception('Call name compute error:')
                # Show just call ID if we failed to render the name above.
                rec.name = str(rec.id)

    def _get_ref(self):
        for rec in self:
            if rec.partner:
                rec.ref = 'res.partner,{}'.format(rec.partner.id)
            else:
                rec.ref = False

    def _get_recording_data(self):
        # Make one query to get all records.
        recordings = self.env['connect.recording'].search([('call', 'in', [k.id for k in self])])
        for rec in self:
            recording = recordings.filtered(lambda x: x.call.id == rec.id)
            if recording:
                rec.recording = recording[0]
                rec.transcript = recording[0].transcript
                rec.recording_icon = '<span class="fa fa-file-sound-o"/>'
                rec.recording_widget = recording[0].recording_widget
            else:
                rec.recording_icon = ''
                rec.transcript = ''
                rec.recording = False
                rec.recording_widget = ''

    def _get_voicemail_widget(self):
        proxy_recordings = self.env['connect.settings'].sudo().get_param('proxy_recordings')
        for rec in self:
            if rec.voicemail_url:
                if proxy_recordings:
                    media_url = '/connect/voicemail/{}'.format(rec.id)
                else:
                    media_url = rec.voicemail_url
                rec.voicemail_widget = '<audio id="sound_file" preload="auto" ' \
                    'controls="controls"> ' \
                    '<source src="{}"/>' \
                    '</audio>'.format(media_url)
            else:
                rec.voicemail_widget = ''

    @api.depends('voicemail_url')
    def _get_voicemail_icon(self):
        for rec in self:
            if rec.voicemail_url:
                rec.voicemail_icon = '<span class="fa fa-envelope-o"/>'
            else:
                rec.voicemail_icon = ''

    @api.depends('duration')
    def _get_duration_human(self):
        for record in self:
            if record.duration is not None:
                minutes = record.duration // 60
                seconds = record.duration % 60
                record.duration_human = '{:02}:{:02}'.format(minutes, seconds)
                record.duration_minutes = record.duration / 60.0
            else:
                record.duration_minutes = 0
                record.duration_human = "00:00"

    def _detect_call_pattern(self):
        """
        Detect the call pattern from explicit channel tagging.
        This replaces the old timing-based inference with explicit source tracking.
        
        Returns:
            'ring_group': Multiple users rang simultaneously (press 0 scenario)  
            'direct_call': Single user called initially (press 1 for extension scenario)
        """
        self.ensure_one()
        
        # First, check if pattern is already set (from gather_action or transfer)
        if self.call_pattern:
            logger.info(f"Call {self.id}: Pattern already set to '{self.call_pattern}'")
            return self.call_pattern
        
        if not self.channels:
            logger.info(f"Call {self.id}: No channels yet for pattern detection")
            return None
            
        # Look at child channels with explicit source tagging
        child_channels = self.channels.filtered(lambda c: c.parent_channel and c.called_pbx_user)
        
        if not child_channels:
            logger.info(f"Call {self.id}: No child channels with users yet for pattern detection")
            return None
            
        logger.info(f"Call {self.id}: Pattern detection with {len(child_channels)} child channels")
        
        # Use explicit tagging instead of counting users
        ring_group_channels = child_channels.filtered(lambda c: c.call_source == 'ring_group')
        direct_call_channels = child_channels.filtered(lambda c: c.call_source == 'direct_call')
        
        if ring_group_channels:
            pattern = 'ring_group'
            logger.info(f"Call {self.id}: Detected pattern '{pattern}' from {len(ring_group_channels)} ring_group channels")
        elif direct_call_channels:
            pattern = 'direct_call'
            logger.info(f"Call {self.id}: Detected pattern '{pattern}' from {len(direct_call_channels)} direct_call channels")
        else:
            # Fallback to old logic if no explicit tagging
            logger.info(f"Call {self.id}: No explicit tagging found, using fallback logic")
            return self._detect_call_pattern_fallback()
        
        return pattern
    
    def _detect_call_pattern_fallback(self):
        """
        Fallback pattern detection using the old timing-based logic.
        Only used when explicit tagging is not available.
        """
        child_channels = self.channels.filtered(lambda c: c.parent_channel and c.called_pbx_user)
        
        initial_called_users = set()
        for channel in child_channels:
            if channel.called_pbx_user and channel.called_pbx_user.user:
                initial_called_users.add(channel.called_pbx_user.user.id)
        
        pattern = 'ring_group' if len(initial_called_users) > 1 else 'direct_call'
        logger.info(f"Call {self.id}: Fallback detected pattern '{pattern}' from {len(initial_called_users)} initially called users")
        return pattern

    def _finalize_call_details(self):
        """
        Called once when all channels are closed to do final call processing.
        This replaces all the complex mid-call status update logic.
        """
        self.ensure_one()
        logger.info(f"=== FINALIZING CALL DETAILS FOR CALL {self.id} ===")
        
        # Step 1: Detect call pattern if not already set
        if not self.call_pattern:
            detected_pattern = self._detect_call_pattern()
            if detected_pattern:
                self.call_pattern = detected_pattern
                logger.info(f"Call {self.id}: Set call pattern to '{detected_pattern}'")
        
        # Step 2: Populate user fields based on detected pattern
        if self.call_pattern == 'direct_call':
            self._populate_user_fields_direct_call()
        elif self.call_pattern == 'ring_group':
            self._populate_user_fields_ring_group()
        else:
            logger.warning(f"Call {self.id}: Unknown call pattern '{self.call_pattern}', using fallback logic")
            self._populate_user_fields_fallback()
        
        # Step 3: Set final call status (simplified logic)
        self._set_final_call_status()
        
        logger.info(f"Call {self.id}: Final status='{self.status}', answered_user='{self.answered_user.login if self.answered_user else None}', completed_by_user='{self.completed_by_user.login if self.completed_by_user else None}', transferred_users={len(self.transferred_users)}")
        
    def _set_final_call_status(self):
        """
        Simplified call status logic based on answered_user field.
        If anyone answered the call, status is 'completed'.
        If no one answered, check for specific error conditions.
        """
        self.ensure_one()
        
        if self.answered_user:
            # Someone answered the call - it's completed regardless of transfers
            self.status = 'completed'
            logger.info(f"Call {self.id}: Status set to 'completed' (answered by {self.answered_user.login})")
        else:
            # No one answered - check for specific error conditions
            channel_statuses = self.channels.mapped('status')
            
            if 'failed' in channel_statuses:
                self.status = 'failed'
                logger.info(f"Call {self.id}: Status set to 'failed' (channel failed)")
            elif 'busy' in channel_statuses:
                self.status = 'busy'
                logger.info(f"Call {self.id}: Status set to 'busy' (channel busy)")
            else:
                self.status = 'no-answer'
                logger.info(f"Call {self.id}: Status set to 'no-answer' (no one answered)")

    def _populate_user_fields_direct_call(self):
        """
        Populate user fields for direct call pattern (press 1 for extension).
        In this pattern, one user is called initially, may transfer to others.
        """
        self.ensure_one()
        logger.info(f"Call {self.id}: Populating user fields for direct call pattern")
        
        # Find all channels with users (completed or not)
        user_channels = self.channels.filtered(lambda c: c.called_pbx_user and c.called_pbx_user.user)
        
        if not user_channels:
            logger.warning(f"Call {self.id}: No channels with users found for direct call")
            return
            
        # Sort by creation order to determine call flow
        user_channels_by_time = user_channels.sorted('create_date')
        
        # ANSWERED USER: First user in the call (earliest created channel)
        first_channel = user_channels_by_time[0]
        if first_channel.called_pbx_user and first_channel.called_pbx_user.user:
            self.answered_user = first_channel.called_pbx_user.user
            self.answered_pbx_user = first_channel.called_pbx_user
            logger.info(f"Call {self.id}: answered_user set to {self.answered_user.login} (first channel)")
        
        # COMPLETED BY USER: User who actually completed the call
        # Look for completed channels specifically
        completed_channels = user_channels.filtered(lambda c: c.status == 'completed')
        
        if completed_channels:
            if self.transferred_users:
                # Transfer occurred - check if transfer recipient completed the call
                transfer_completed_channels = completed_channels.filtered(
                    lambda c: c.called_pbx_user.user in self.transferred_users
                )
                if transfer_completed_channels:
                    # Transfer recipient completed the call
                    final_channel = transfer_completed_channels.sorted('write_date')[-1]
                    self.completed_by_user = final_channel.called_pbx_user.user
                    logger.info(f"Call {self.id}: completed_by_user set to transfer recipient {self.completed_by_user.login}")
                else:
                    # Transfer failed, nobody completed the call
                    self.completed_by_user = False
                    logger.info(f"Call {self.id}: completed_by_user left empty (transfer failed - nobody completed)")
            else:
                # No transfer - original answerer completed
                self.completed_by_user = self.answered_user
                logger.info(f"Call {self.id}: completed_by_user set to original answerer {self.completed_by_user.login} (no transfer)")

    def _populate_user_fields_ring_group(self):
        """
        Populate user fields for ring group pattern (press 0 - ring all reps).
        In this pattern, multiple users are rung simultaneously, first to answer gets it.
        """
        self.ensure_one()
        logger.info(f"Call {self.id}: Populating user fields for ring group pattern")
        
        # Find channels tagged as ring_group with users
        ring_group_channels = self.channels.filtered(lambda c: c.call_source == 'ring_group' and c.called_pbx_user and c.called_pbx_user.user)
        
        if not ring_group_channels:
            logger.warning(f"Call {self.id}: No ring_group channels with users found")
            return
        
        # Find which ring group channel was completed (answered)
        completed_ring_channels = ring_group_channels.filtered(lambda c: c.status == 'completed')
        
        if not completed_ring_channels:
            logger.info(f"Call {self.id}: No completed ring_group channels found - no one answered")
            return
            
        # ANSWERED USER: Person who answered from ring group (should be only one)
        if len(completed_ring_channels) > 1:
            # Multiple completed? Use earliest ID (creation order)
            answered_channel = completed_ring_channels.sorted('id')[0]
            logger.warning(f"Call {self.id}: Multiple completed ring_group channels, using earliest: {answered_channel.id}")
        else:
            answered_channel = completed_ring_channels[0]
            
        self.answered_user = answered_channel.called_pbx_user.user
        self.answered_pbx_user = answered_channel.called_pbx_user
        logger.info(f"Call {self.id}: answered_user set to {self.answered_user.login} (answered from ring group)")
        
        # COMPLETED BY USER: Person who completed the call
        if self.transferred_users:
            # Check if transfer recipient completed the call
            all_completed_channels = self.channels.filtered(lambda c: c.status == 'completed' and c.called_pbx_user and c.called_pbx_user.user)
            transfer_completed_channels = all_completed_channels.filtered(
                lambda c: c.called_pbx_user.user in self.transferred_users
            )
            if transfer_completed_channels:
                # Transfer recipient completed - use most recent transfer completion
                final_channel = transfer_completed_channels.sorted('id')[-1]
                self.completed_by_user = final_channel.called_pbx_user.user
                logger.info(f"Call {self.id}: completed_by_user set to transfer recipient {self.completed_by_user.login}")
            else:
                # Transfer failed, nobody completed the call
                self.completed_by_user = False
                logger.info(f"Call {self.id}: completed_by_user left empty (transfer failed - nobody completed)")
        else:
            # No transfer - answered user also completed
            self.completed_by_user = self.answered_user
            logger.info(f"Call {self.id}: completed_by_user set to answerer {self.completed_by_user.login} (no transfer)")

    def _populate_user_fields_fallback(self):
        """
        Fallback user field population when pattern detection fails.
        Uses simple logic similar to original approach.
        """
        self.ensure_one()
        logger.info(f"Call {self.id}: Using fallback user field population")
        
        # Find any completed channels with users
        completed_channels = self.channels.filtered(lambda c: c.status == 'completed' and c.called_pbx_user and c.called_pbx_user.user)
        
        if completed_channels:
            sorted_channels = completed_channels.sorted('write_date')
            
            # Set answered_user to first completed channel
            first_channel = sorted_channels[0]
            self.answered_user = first_channel.called_pbx_user.user
            self.answered_pbx_user = first_channel.called_pbx_user
            
            # Set completed_by_user to last completed channel
            last_channel = sorted_channels[-1] 
            self.completed_by_user = last_channel.called_pbx_user.user
            
            logger.info(f"Call {self.id}: Fallback - answered_user={self.answered_user.login}, completed_by_user={self.completed_by_user.login}")

    def add_transferred_user(self, user):
        """
        Add a user to the transferred_users field when a transfer is initiated.
        Called from transfer.py when transfers actually happen.
        This provides explicit transfer tracking instead of inferring from channel states.
        """
        self.ensure_one()
        if user and hasattr(user, 'id'):
            current_transfer_ids = self.transferred_users.ids
            if user.id not in current_transfer_ids:
                self.transferred_users = [(4, user.id)]  # Add user to many2many
                logger.info(f"Call {self.id}: Transfer initiated to {user.login} (added to transferred_users)")
                
                # Update call pattern if needed - transfers can help us understand the call type
                if not self.call_pattern:
                    detected_pattern = self._detect_call_pattern()
                    if detected_pattern:
                        self.call_pattern = detected_pattern
                        logger.info(f"Call {self.id}: Pattern detection triggered by transfer: '{detected_pattern}'")

    def write(self, vals):
        return super().write(vals)

    @api.model
    def on_call_status(self, params):
        self = self.sudo()
        # Create channel
        channel = self.env['connect.channel'].on_call_status(params)
        if not channel:
            logger.error('No channel returned from on_call_status!')
            return False
        if not channel.parent_channel and not channel.call:
            # Create a new call.
            if channel.technical_direction == 'outbound-api':
                # Click2call originated call.
                debug(self, 'outbound-api channel direction.')
                direction = 'outgoing'
            elif channel.technical_direction == 'inbound' and channel.caller_pbx_user:
                # Outgoing call from SIP or Client.
                debug(self, 'inbound channel direction with caller_pbx_user.')
                direction = 'outgoing'
            elif channel.technical_direction == 'inbound' and not channel.caller_pbx_user:
                # Incoming DID call
                debug(self, 'inbound channel direction without caller_pbx_user. Assuming DID call.')
                direction = 'incoming'
            else:
                # Default
                debug(self, 'Setting default call direction to outgoing.')
                direction = 'outgoing'
            call = self.with_context(tracking_disable=True).create({
                'partner': channel.partner.id,
                'called': channel.called_number,
                'caller': channel.caller_number,
                'status': channel.status,
                'caller_pbx_user': channel.caller_pbx_user.id,
                'caller_user': channel.caller_user.id,
                'direction': direction,
            })
            channel.call = call
        elif channel.parent_channel and channel.parent_channel.call:
            # Secondary channel, assign the call from the parent.
            channel.call = channel.parent_channel.call
            if channel.caller_pbx_user and channel.parent_channel.called_pbx_user:
                channel.call.direction = 'internal'
            elif channel.called_pbx_user and channel.parent_channel.caller_pbx_user:
                channel.call.direction = 'internal'
                
        # Set called from 2nd call leg for click2call external calls.
        if channel.parent_channel and channel.parent_channel.technical_direction == 'outbound-api':
            channel.call.called = channel.called_number
        # Set called users
        if channel.called_user:
            channel.call.called_users = [(4, channel.called_user.id)]
        if channel.called_pbx_user:
            channel.call.called_pbx_users = [(4, channel.called_pbx_user.id)]
        # Check if we need to set a partner from child channel
        if not channel.call.partner and channel.partner:
            channel.call.partner = channel.partner
            
        # Update call duration based on all channels
        if channel.call:
            # Set call duration as sum of all channel durations
            if channel.call.channels:
                total_duration = sum(channel.call.channels.mapped('duration') or [0])
                channel.call.duration = total_duration
                logger.debug(f"Call {channel.call.id} total duration updated to {total_duration} seconds from {len(channel.call.channels)} channels")
            
            # PATTERN DETECTION: Use explicit tagging from gather_action or fallback logic
            # Pattern should already be set by gather_action, but check in case it wasn't
            if not channel.call.call_pattern:
                detected_pattern = channel.call._detect_call_pattern()
                if detected_pattern:
                    channel.call.call_pattern = detected_pattern
                    logger.info(f"Call {channel.call.id}: Pattern detection set to '{detected_pattern}'")
        
        if (channel.call.direction == 'incoming' and params.get('CallStatus') == 'initiated' and
                params.get('To').startswith('sip:')):
            # Desktop notification only for SIP calls.
            channel.connect_notify()
        # Register call only when ALL channels have ended (call truly finished)
        # Check if this channel ending means the entire call is complete
        all_channels_ended = all(ch.status in CALL_END_STATUSES for ch in channel.call.channels)
        if all_channels_ended and params.get('CallStatus') in CALL_END_STATUSES:
            # NOW do all the final call processing
            channel.call._finalize_call_details()
            self.register_call(channel, params)
        # Reload call view
        self.env['connect.settings'].connect_reload_view('connect.call')
        if params.get('ErrorCode') and params.get('ErrorCode') not in IGNORE_ERROR_CODES:
            channel.call.update({
                'has_error': True,
                'error_code': params.get('ErrorCode'),
                'error_message': params.get('ErrorMessage')
            })
            # Notify caller user on errors on outgoing calls.
            user = channel.caller_user or channel.call.caller_user
            if channel.call.direction == 'outgoing' and user:
                if 'No International Permission' in params.get('ErrorMessage', ''):
                    message_text = re.sub(
                        r'(https?://\S+)',
                        r'<strong><a target="_blank" href="\1">your Twilio Console</a></strong>',
                        params.get('ErrorMessage', ''))
                else:
                    message_text = params.get('ErrorMessage', '')
                self.env['connect.settings'].connect_notify(
                    notify_uid=user.id,
                    title="Call Error",
                    message=message_text,
                    warning=True,
                )
        return channel.call.id

    @api.model
    def on_vm_recording_status(self, params):
        debug(self.sudo(), 'On recording status: %s' % json.dumps(params, indent=2))
        channel = self.sudo().env['connect.channel'].search([('sid', '=', params['CallSid'])])
        if channel and channel.call:
            channel.call.write({
                'voicemail_url': params.get('RecordingUrl'),
                'voicemail_duration': int(params.get('RecordingDuration'))
            })
        return True

    @api.model
    def on_call_action(self, params):
        debug(self, 'On call action: %s' % params)
        
        # Check if this is a Dial action webhook with transfer completion data
        if 'DialCallSid' in params and 'DialCallStatus' in params:
            logger.info(f"Processing Dial action webhook for transfer completion")
            logger.info(f"DialCallSid: {params.get('DialCallSid')}, DialCallStatus: {params.get('DialCallStatus')}")
            
            # For blind transfers, we need to update the existing transfer recipient channel
            # instead of creating a new channel with the DialCallSid
            try:
                self._process_transfer_completion(params)
                logger.info(f"Successfully processed transfer completion")
            except Exception as e:
                logger.error(f"Failed to process transfer completion: {e}")
        
        return '<Response><Hangup/></Response>'

    def _process_transfer_completion(self, params):
        """
        Process Dial action webhook to update existing transfer recipient channel
        instead of creating new channels that break call status logic.
        
        Key insight: The DialCallSid represents the transfer recipient's actual call,
        we need to find the recipient by matching this SID to existing channels.
        """
        dial_call_sid = params.get('DialCallSid')
        dial_status = params.get('DialCallStatus') 
        original_call_sid = params.get('CallSid')  # The original call that initiated transfer
        
        logger.info(f"=== PROCESSING TRANSFER COMPLETION ===")
        logger.info(f"Original CallSid: {original_call_sid}")
        logger.info(f"DialCallSid: {dial_call_sid}")  
        logger.info(f"DialCallStatus: {dial_status}")
        logger.info(f"All webhook params: {params}")
        
        # Find the original call/channel that initiated the transfer
        original_channel = self.env['connect.channel'].search([('sid', '=', original_call_sid)], limit=1)
        if not original_channel or not original_channel.call:
            logger.warning(f"Could not find original channel for transfer CallSid: {original_call_sid}")
            return
            
        call = original_channel.call
        logger.info(f"Found call {call.id} for transfer processing")
        
        # STRATEGY 1: Find recipient channel by matching DialCallSid to existing channel SIDs
        # This should work because the DialCallSid is the actual call SID for the transfer recipient
        recipient_channel = call.channels.filtered(lambda c: c.sid == dial_call_sid)
        
        if recipient_channel:
            logger.info(f"STRATEGY 1 SUCCESS: Found recipient channel {recipient_channel[0].id} by matching DialCallSid")
            recipient_channel = recipient_channel[0]
        else:
            logger.info(f"STRATEGY 1 FAILED: No channel found with SID {dial_call_sid}")
            
            # STRATEGY 1B: Create the missing transfer channel since Twilio didn't send us a webhook for it
            # This happens when transfers create new calls that we don't get webhook notifications for
            logger.info(f"STRATEGY 1B: Creating missing transfer channel for SID {dial_call_sid}")
            parent_channel = call.channels.filtered(lambda c: not c.parent_channel)[0]  # Parent channel
            
            # Find transfer target user from transferred_users
            if call.transferred_users:
                transfer_target_user = call.transferred_users[-1]  # Most recent transfer
                # Find the PBX user for this Odoo user
                pbx_user = self.env['connect.user'].search([('user', '=', transfer_target_user.id)], limit=1)
                
                if pbx_user:
                    # Create the missing transfer channel
                    channel_data = {
                        'sid': dial_call_sid,
                        'call': call.id,
                        'parent_channel': parent_channel.id,
                        'technical_direction': 'outbound-dial',
                        'status': dial_status,
                        'duration': int(params.get('DialCallDuration', 0)),
                        'called_pbx_user': pbx_user.id,
                        'called_user': transfer_target_user.id,
                        'call_source': 'transfer',  # Explicitly tag as transfer
                        'caller': original_channel.caller,
                        'called': pbx_user.uri
                    }
                    
                    recipient_channel = self.env['connect.channel'].create(channel_data)
                    logger.info(f"STRATEGY 1B SUCCESS: Created transfer channel {recipient_channel.id} for {transfer_target_user.login}")
                else:
                    logger.warning(f"STRATEGY 1B FAILED: Could not find PBX user for {transfer_target_user.login}")
            
            if not recipient_channel:
                # STRATEGY 2: Find the most recent channel that's NOT the transfer initiator
                # Based on the logs, Jason's channel should be the most recent one created
                child_channels = call.channels.filtered(lambda c: c.parent_channel)
                if child_channels:
                    # Sort by ID (creation order) and look for the most recent one that's not completed
                    potential_recipients = child_channels.filtered(lambda c: c.status in ['no-answer', 'ringing', 'in-progress'])
                    if potential_recipients:
                        recipient_channel = potential_recipients.sorted('id', reverse=True)[0]
                        logger.info(f"STRATEGY 2: Using most recent non-completed channel {recipient_channel.id} as recipient")
                    else:
                        logger.warning(f"STRATEGY 2 FAILED: No suitable recipient channels found")
                    return
            else:
                logger.warning(f"STRATEGY 2 FAILED: No child channels found")
                return
        
        if recipient_channel and recipient_channel.called_pbx_user:
            logger.info(f"Transfer recipient identified: {recipient_channel.called_pbx_user.name} (Channel {recipient_channel.id})")
            
            # Map DialCallStatus to proper channel status
            if dial_status == 'completed':
                new_status = 'completed'
                duration = int(params.get('DialCallDuration', 0))
            elif dial_status == 'busy':
                new_status = 'busy'
                duration = 0
            elif dial_status == 'no-answer':
                new_status = 'no-answer'  
                duration = 0
            elif dial_status == 'failed':
                new_status = 'failed'
                duration = 0
            else:
                new_status = dial_status
                duration = int(params.get('DialCallDuration', 0))
            
            logger.info(f"Updating channel {recipient_channel.id} status from '{recipient_channel.status}' to '{new_status}' with duration {duration}")
            
            # Update the existing channel with transfer completion data
            recipient_channel.write({
                'status': new_status,
                'duration': duration,
            })
            
            # Note: We don't do final processing here because not all channels may be closed yet
            # Final processing will happen when all channels are closed in on_call_status
            logger.info(f"Transfer completion processed for call {call.id} - final processing will occur when all channels close")
                
        else:
            logger.warning(f"Could not identify transfer recipient channel or PBX user")
            
        logger.info(f"=== TRANSFER COMPLETION PROCESSING COMPLETE ===")

    def register_call(self, channel, params):
        try:
            notify_users = []
            # Construct message from lines
            message = [channel.call.status.capitalize(), channel.call.direction,
                       'call at {}, '.format(channel.create_date.strftime('%Y-%m-%d %H:%M:%S'))]
            if channel.call.caller_user:
                message.append('caller: {}, '.format(channel.call.caller_user.name))
            if channel.call.duration:
                message.append('duration: {}, '.format(channel.call.duration_human))
            if channel.call.answered_user:
                message.append('answered by: {}, '.format(channel.call.answered_user.name))
            if channel.call.called_users:
                message.append('dialed users: {}, '.format(', '.join(k.name for k in channel.call.called_users)))
                # Missed call notification, filter users who have it enabled.
                # If transfers occurred, only notify transfer recipients for missed calls
                # Otherwise, notify all called users (original behavior)
                users_to_check = channel.call.transferred_users if channel.call.transferred_users else channel.call.called_users
                for user in users_to_check:
                    if user.connect_user[0].missed_calls_notify:
                        notify_users.append(user)
            # Register call at partner.
            if channel.call.partner:
                message.insert(3, 'partner: {}, '.format(channel.call.partner.name))
                final_message = ' '.join(message)
                if final_message.endswith(', '):
                    final_message = final_message[:-2] + '.'
                channel.call.register_call_post_message(
                    channel.call.partner, body=final_message, subtype_xmlid='mail.mt_note')
            # Register call to users
            statuses = ['completed']
            # Since register_call() now only runs when call is truly finished, we can safely send missed call notifications
            if channel.call.direction == 'incoming' and channel.call.status not in statuses and notify_users:
                debug(self, 'Missed call notification to users: {}'.format(notify_users))
                final_message = ' '.join(message)
                if final_message.endswith(', '):
                    final_message = final_message[:-2] + '.'
                channel.call.register_call_post_message(
                    channel.call,
                    subtype_xmlid='mail.mt_comment',
                    subject=channel.call.name,
                    body=final_message,
                    partner_ids=[k.partner_id.id for k in notify_users]
                )
        except Exception as e:
            logger.exception('Register call error:', e)

    def register_call_post_message(self, obj, **kwargs):
        try:
            obj.with_user(SUPERUSER_ID).with_context(mail_create_nosubscribe=False).message_post(**kwargs)
        except Exception:
            logger.exception('Register call error: ')

    def register_summary_to_rec(self, rec, summary):
        try:
            if release.version_info[0] < 14:
                rec.sudo(SUPERUSER_ID).message_post(body=summary)
            else:
                rec.with_user(SUPERUSER_ID).message_post(body=summary)
        except Exception as e:
            logger.error('Cannot register summary: %s', e)

    @api.constrains('summary')
    def register_partner_call_summary(self):
        reload_view = False
        register_summary = self.env['connect.settings'].sudo().get_param('register_summary')
        if not register_summary:
            return
        for rec in self:
            if rec.partner and rec.summary:
                self.register_summary_to_rec(rec.partner, rec.summary)
                reload_view = True
        # Reload changed view.
        if reload_view:
            # Reload the view of res.partner
            self.env['connect.settings'].connect_reload_view('res.partner')

    def create_partner_button(self):
        self.ensure_one()
        name_number = self.caller if self.direction == 'incoming' else self.called
        context = {
            'connect_call_id': self.id,
            'default_phone': name_number,
        }
        # Check if it's a click on a call with existing partner (linking)
        if not self.partner:
            partner = self.env['res.partner'].get_partner_by_number(name_number)
            if partner:
                self.sudo().partner = partner  # Use sudo as user has not access to write to call.
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'res.partner',
            'res_id': self.partner.id,
            'name': self.partner.name if self.partner else 'New Partner',
            'view_mode': 'form',
            'target': 'current',
            'context': context,
        }

    def transfer_button(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'connect.transfer_wizard',
            'view_mode': 'form',
            'target': 'new',
            'name': 'Transfer Wizard'
        }

    def transfer(self, user=None):
        self.ensure_one()
        if False:  # self.status not in ['in-progress', 'ringing']:
            logger.warning('Call not in progress, cannot transfer')
            return
        # Get the PBX user doing trasnfer
        if not user:
            user = self.env.user.connect_user
            user = self.channels[0].caller_pbx_user or self.channels[0].called_pbx_user
        """
        # Case 1: User is on primary channel.
        primary_channel = self.channels.filtered(lambda x: x.parent_channel == False)
        if primary_channel and primary_channel.caller_pbx_user:
            print(111, 'PRIMARY CHANNEL CALLER', primary_channel)
        elif primary_channel and primary_channel.called_pbx_user:
            print(1111, 'PRIMARY CHANNEL CALLED', primary_channel)
        # Find current user on all channels.
        print(111111, self.channels)
        """
        user_channel = self.channels.filtered(
            lambda x: (x.caller_pbx_user == user or x.called_pbx_user == user))
        if not user_channel:
            logger.warning('Cannot get user channel for call %s for user %s', self.id, user.name)
            return
        other_channel = self.channels - user_channel
        if len(other_channel) != 1:
            logger.warning('Cannot transfer call, number of other channels: %s', len(other_channel))
            return
        client = self.env['connect.settings'].get_client()
        conf_id = uuid.uuid4().hex

        def transfer_other():
            # Put other channel into conference.
            response = VoiceResponse()
            response.say('Transfer')
            dial = Dial()
            dial.conference('user-{}-{}'.format(user.id, conf_id))
            response.append(dial)
            # response.play('http://com.twilio.music.classical.s3.amazonaws.com/BusyStrings.mp3')
            client.calls(other_channel.sid).update(twiml=response)

        def transfer_user():
            # Dial a new call party.
            response = VoiceResponse()
            response.say('Transfer')
            dial = Dial()
            sip = Sip('sip:user@devmax17.sip.twilio.com')
            # dial.conference('user-{}-{}'.format(user.id,  conf_id))
            dial.append(sip)
            response.append(dial)
            # response.play('http://com.twilio.music.classical.s3.amazonaws.com/BusyStrings.mp3')
            client.calls(user_channel.sid).update(twiml=response)

        transfer_user()
        transfer_other()

    def redial(self):
        self.ensure_one()
        self.env['connect.settings'].originate_call(
            number=self.called if self.direction == 'outgoing' else self.caller,
        )

    @api.model
    def get_widget_calls(self, domain, limit=None, offset=0, order='id desc', fields=[]):
        calls = self.search(domain, offset, limit, order)
        payload = []
        read_fields = self.get_widget_fields()
        if isinstance(fields, list):
            read_fields.extend(fields)
        for call in calls:
            call_data = call.read(read_fields)[0]
            if call.called_users:
                call_data.update({'called_users': list(call.called_users.read(['id', 'name'])[0].values())})
            payload.append(call_data)
        return payload

    def get_widget_fields(self):
        return [
            "id",
            "called",
            "caller",
            "caller_user",
            "called_users",
            "partner",
            "create_date",
            "direction"
        ]
