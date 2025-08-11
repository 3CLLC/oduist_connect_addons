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
    transferred_users = fields.Many2many(
        'res.users', 
        relation='connect_call_transferred_users_rel',
        column1='call_id', 
        column2='user_id',
        string='Transferred Users', 
        readonly=True,
        help='Users who received this call via transfer (in chronological order)'
    )
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

    def _update_call_status_from_channels(self):
        """
        Update the call's status based on its channels' statuses.
        For calls with multiple channels (transfers), use the most meaningful status.
        Priority: completed (by user) > in-progress (by user) > failed > busy > no-answer > canceled > other statuses
        
        A call is only "completed" if an actual user answered it, not just voicemail.
        An "in-progress" call with a user means someone has answered and is actively on the call.
        """
        self.ensure_one()
        
        logger.info(f"=== DEBUGGING CALL STATUS FOR CALL {self.id} ===")
        logger.info(f"Call {self.id} current status: {self.status}")
        
        if not self.channels:
            logger.warning(f"Call {self.id} has no channels to determine status from")
            return
        
        # Log all channel statuses and details
        logger.info(f"Call {self.id} analyzing {len(self.channels)} channels for status determination:")
        for i, channel in enumerate(self.channels.sorted('id')):
            parent_info = f"parent={channel.parent_channel.id}" if channel.parent_channel else "root"
            caller_pbx = channel.caller_pbx_user.name if channel.caller_pbx_user else "None"
            called_pbx = channel.called_pbx_user.name if channel.called_pbx_user else "None"
            called_user = channel.called_user.login if channel.called_user else "None"
            
            logger.info(f"  Channel {i+1}: ID={channel.id}, status={channel.status}, {parent_info}")
            logger.info(f"    caller_pbx_user={caller_pbx}, called_pbx_user={called_pbx}")
            logger.info(f"    called_user={called_user}, duration={channel.duration}")
        
        # Define status priority (higher number = higher priority)
        # Note: in-progress is a channel status, not a call status
        status_priority = {
            'completed': 5,
            'failed': 4, 
            'busy': 3,
            'no-answer': 2,
            'canceled': 1,
        }
        
        # Get all channel statuses
        channel_statuses = self.channels.mapped('status')
        logger.info(f"Call {self.id} all channel statuses: {channel_statuses}")
        
        # Check if any channel was actually answered by a user (completed OR in-progress)
        user_answered_channels = self.channels.filtered(
            lambda c: c.status in ['completed', 'in-progress'] and c.called_pbx_user
        )
        
        logger.info(f"Call {self.id} found {len(user_answered_channels)} user-answered channels (completed or in-progress):")
        for channel in user_answered_channels.sorted('id'):
            user_name = channel.called_pbx_user.name
            odoo_user = channel.called_pbx_user.user.login if channel.called_pbx_user.user else "None"
            logger.info(f"  User-answered channel: ID={channel.id}, status={channel.status}, pbx_user={user_name}, odoo_user={odoo_user}")
        
        if user_answered_channels:
            # A user actually answered the call
            # For call status: use 'completed' only when all user channels are completed
            completed_user_channels = user_answered_channels.filtered(lambda c: c.status == 'completed')
            
            if len(completed_user_channels) == len(user_answered_channels):
                # All user-answered channels are completed
                new_status = 'completed'
                logger.info(f"Call {self.id} -> STATUS DECISION: completed (all user channels completed)")
            else:
                # Some channels still in-progress, but since a user answered, use highest priority from remaining statuses
                # but ensure we set answered user now for transfer detection
                remaining_statuses = [s for s in channel_statuses if s != 'in-progress']
                if remaining_statuses:
                    current_priority = 0
                    new_status = self.status or 'no-answer'
                    
                    for status in remaining_statuses:
                        if status in status_priority:
                            priority = status_priority[status]
                            if priority > current_priority:
                                current_priority = priority
                                new_status = status
                    
                    logger.info(f"Call {self.id} -> STATUS DECISION: {new_status} (user answered, but call not fully completed)")
                else:
                    # Only in-progress channels remain - keep current status or set to ringing
                    new_status = self.status or 'ringing'
                    logger.info(f"Call {self.id} -> STATUS DECISION: {new_status} (user answered, call still active)")
        else:
            # No user answered - check for voicemail or determine best status
            voicemail_channels = self.channels.filtered(
                lambda c: c.status == 'completed' and not c.called_pbx_user
            )
            
            logger.info(f"Call {self.id} found {len(voicemail_channels)} voicemail channels:")
            for channel in voicemail_channels.sorted('id'):
                logger.info(f"  Voicemail channel: ID={channel.id}, status={channel.status}")
            
            if voicemail_channels:
                # Call went to voicemail only
                logger.info(f"Call {self.id} -> STATUS DECISION: no-answer (voicemail only)")
                new_status = 'no-answer'  # Voicemail = no human answered
            else:
                # Find the highest priority status among non-completed channels
                current_priority = 0
                new_status = self.status or 'no-answer'  # Default fallback
                
                logger.info(f"Call {self.id} no user answers or voicemail, checking priority statuses:")
                for status in channel_statuses:
                    if status in status_priority:
                        priority = status_priority[status]
                        logger.info(f"  Status '{status}' has priority {priority}")
                        if priority > current_priority:
                            current_priority = priority
                            new_status = status
                            logger.info(f"    -> New highest priority status: {status}")
                
                logger.info(f"Call {self.id} -> STATUS DECISION: {new_status} (highest priority status)")
        
        # Only update if status actually changed
        if self.status != new_status:
            logger.info(f"Call {self.id} STATUS CHANGE: '{self.status}' -> '{new_status}'")
            self.status = new_status
        else:
            logger.info(f"Call {self.id} STATUS UNCHANGED: remains '{self.status}'")
        
        # IMPORTANT: Set answered user whenever we detect user-answered channels,
        # regardless of whether the call status changed
        if user_answered_channels:
            logger.info(f"Call {self.id} updating answered user (user channels detected)")
            self._update_answered_user_from_channels()
        
        logger.info(f"=== END CALL STATUS DEBUG FOR CALL {self.id} ===")

    def _update_answered_user_from_channels(self):
        """
        Set the answered user based on the final/last channel that was answered by a user.
        For transfers, this should be the user who ultimately handled the call.
        Now handles both completed and in-progress channels.
        """
        self.ensure_one()
        
        logger.info(f"=== DEBUGGING ANSWERED USER UPDATE FOR CALL {self.id} ===")
        logger.info(f"Call {self.id} current answered_user: {self.answered_user.login if self.answered_user else 'None'}")
        logger.info(f"Call {self.id} current answered_pbx_user: {self.answered_pbx_user.name if self.answered_pbx_user else 'None'}")
        
        # Find channels that were answered by users (completed OR in-progress with pbx_user)
        answered_channels = self.channels.filtered(
            lambda c: c.status in ['completed', 'in-progress'] and c.called_pbx_user
        )
        
        logger.info(f"Call {self.id} found {len(answered_channels)} answered channels (completed or in-progress):")
        for channel in answered_channels.sorted('id'):
            pbx_user = channel.called_pbx_user.name if channel.called_pbx_user else "None"
            odoo_user = channel.called_pbx_user.user.login if channel.called_pbx_user and channel.called_pbx_user.user else "None"
            logger.info(f"  Answered channel: ID={channel.id}, status={channel.status}, called_pbx_user={pbx_user}, odoo_user={odoo_user}")
        
        if not answered_channels:
            logger.warning(f"Call {self.id} no answered channels found (neither completed nor in-progress with pbx_user)")
            logger.info(f"=== END ANSWERED USER DEBUG FOR CALL {self.id} ===")
            return
        
        # Get the last (newest) answered channel - this represents the final answerer
        final_channel = answered_channels.sorted(key='id', reverse=True)[0]
        logger.info(f"Call {self.id} final answered channel: ID={final_channel.id}, status={final_channel.status}")
        
        # Set answered PBX user from the final channel
        if final_channel.called_pbx_user:
            old_answered_pbx_user = self.answered_pbx_user.name if self.answered_pbx_user else "None"
            self.answered_pbx_user = final_channel.called_pbx_user
            logger.info(f"Call {self.id} answered_pbx_user: {old_answered_pbx_user} -> {self.answered_pbx_user.name}")
            
            # Set answered Odoo user if PBX user has associated Odoo user
            if final_channel.called_pbx_user.user:
                old_answered_user = self.answered_user.login if self.answered_user else "None"
                self.answered_user = final_channel.called_pbx_user.user
                logger.info(f"Call {self.id} answered_user: {old_answered_user} -> {self.answered_user.login}")
            else:
                logger.warning(f"Final channel {final_channel.id} called_pbx_user has no linked Odoo user")
        else:
            logger.warning(f"Final channel {final_channel.id} has no called_pbx_user")
        
        logger.info(f"=== END ANSWERED USER DEBUG FOR CALL {self.id} ===")

    def _update_transferred_users_from_channels(self):
        """
        Update the transferred_users field based on actual user-to-user transfers.
        Enhanced with additional debugging to understand transfer detection.
        """
        self.ensure_one()
        
        logger.info(f"=== ENHANCED TRANSFER DEBUG FOR CALL {self.id} ===")
        logger.info(f"Call {self.id} current status: {self.status}")
        logger.info(f"Call {self.id} current answered_user: {self.answered_user.login if self.answered_user else 'None'}")
        logger.info(f"Call {self.id} current transferred_users: {[u.login for u in self.transferred_users]}")
        
        # Log all channels with enhanced information
        logger.info(f"Call {self.id} has {len(self.channels)} total channels:")
        for i, channel in enumerate(self.channels.sorted('id')):
            parent_info = f"parent_channel={channel.parent_channel.id}" if channel.parent_channel else "parent_channel=None"
            caller_user = channel.caller_pbx_user.name if channel.caller_pbx_user else "None"
            called_user = channel.called_pbx_user.name if channel.called_pbx_user else "None"
            called_odoo_user = channel.called_user.login if channel.called_user else "None"
            
            logger.info(f"  Channel {i+1}: ID={channel.id}, status={channel.status}, {parent_info}")
            logger.info(f"    caller_pbx_user={caller_user}, called_pbx_user={called_user}")
            logger.info(f"    called_user(Odoo)={called_odoo_user}")
            logger.info(f"    technical_direction={channel.technical_direction}")
            logger.info(f"    duration={channel.duration}, created_at={channel.create_date}")
            
            # ENHANCED: Look for additional clues about transfers
            # Check if this channel has any special attributes that indicate transfers
            if hasattr(channel, 'transfer_initiated_by'):
                logger.info(f"    transfer_initiated_by={channel.transfer_initiated_by}")
            if hasattr(channel, 'transfer_type'):
                logger.info(f"    transfer_type={channel.transfer_type}")
            if hasattr(channel, 'call_sid'):
                logger.info(f"    call_sid={channel.call_sid}")
            
            # Log channel relationships and timing patterns
            if channel.parent_channel:
                time_diff = (channel.create_date - channel.parent_channel.create_date).total_seconds()
                logger.info(f"    time_since_parent_created={time_diff}s")
                
                # Check if parent had any activity before this child was created
                parent_had_activity = channel.parent_channel.status not in ['initiated', 'ringing']
                logger.info(f"    parent_had_activity_before_child={parent_had_activity}")
        
        # ENHANCED: Look for transfer-related records in other models
        logger.info(f"=== CHECKING FOR TRANSFER RECORDS ===")
        
        # Check if there are any transfer wizard records associated with this call
        if hasattr(self.env, 'connect.transfer_wizard'):
            transfer_records = self.env['connect.transfer_wizard'].search([
                '|', ('call_id', '=', self.id), ('session_id', 'in', self.channels.mapped('call_sid'))
            ])
            logger.info(f"Found {len(transfer_records)} transfer wizard records")
            for transfer in transfer_records:
                logger.info(f"  Transfer record: session={transfer.session_id}, target={transfer.phone_number}")
        
        # ENHANCED: Analyze channel creation patterns
        logger.info(f"=== ANALYZING CHANNEL PATTERNS ===")
        
        # Group channels by creation time windows
        root_channels = self.channels.filtered(lambda c: not c.parent_channel)
        child_channels = self.channels.filtered(lambda c: c.parent_channel)
        
        logger.info(f"Root channels: {len(root_channels)}")
        logger.info(f"Child channels: {len(child_channels)}")
        
        # For child channels, analyze their creation timing relative to their parent's status changes
        simultaneous_threshold = 5  # seconds - channels created within this window might be simultaneous
        
        for channel in child_channels:
            if channel.parent_channel:
                time_diff = (channel.create_date - channel.parent_channel.create_date).total_seconds()
                
                # Classify channel creation timing
                if time_diff < simultaneous_threshold:
                    timing_classification = "SIMULTANEOUS"
                else:
                    timing_classification = "SEQUENTIAL" 
                    
                logger.info(f"  Channel {channel.id}: {timing_classification} (created {time_diff:.1f}s after parent)")
                
                # Check what the parent channel status was when this child was created
                # Note: We can't know historical status, but we can infer from patterns
                if channel.called_user and channel.parent_channel.status == 'ringing':
                    logger.info(f"    Pattern: Child created while parent still ringing - likely SIMULTANEOUS_RING")
                elif channel.called_user and time_diff > simultaneous_threshold:
                    logger.info(f"    Pattern: Child created well after parent - likely TRANSFER")
        
        # ENHANCED: Look for specific transfer indicators
        logger.info(f"=== TRANSFER INDICATORS ===")
        
        # Indicator 1: Check if answered user appears in later channels as caller
        if self.answered_user:
            answered_user_as_caller = self.channels.filtered(
                lambda c: c.caller_pbx_user and c.caller_pbx_user.user == self.answered_user
            )
            if answered_user_as_caller:
                logger.info(f"INDICATOR: Answered user {self.answered_user.login} appears as caller in channels: {answered_user_as_caller.mapped('id')}")
        
        # Indicator 2: Check for channels created significantly after call start
        call_start = min(self.channels.mapped('create_date'))
        for channel in child_channels:
            seconds_since_start = (channel.create_date - call_start).total_seconds()
            if seconds_since_start > 10:  # More than 10 seconds after call start
                logger.info(f"INDICATOR: Channel {channel.id} created {seconds_since_start:.1f}s after call start - possible transfer")
        
        # Continue with existing logic but with enhanced context
        transferred_user_ids = []
        
        if not self.answered_user:
            logger.info(f"Call {self.id} has no answered_user, no transfers possible - clearing transferred_users")
            self.transferred_users = [(5, 0, 0)]
            return
        
        # Find answered channels
        answered_channels = self.channels.filtered(
            lambda c: c.status == 'completed' and c.called_pbx_user and c.called_pbx_user.user == self.answered_user
        )
        
        if not answered_channels:
            logger.info(f"Call {self.id} no completed channels found for answered_user - clearing transferred_users")
            self.transferred_users = [(5, 0, 0)]
            return
        
        first_answer_channel = answered_channels.sorted('id')[0]
        first_answer_time = first_answer_channel.id
        logger.info(f"Call {self.id} first answered at channel ID: {first_answer_time}")
        
        # ENHANCED: Apply more sophisticated transfer detection
        logger.info(f"=== SOPHISTICATED TRANSFER DETECTION ===")
        
        potential_transfer_channels = self.channels.filtered(
            lambda c: (
                c.parent_channel and  # Has a parent (is a child channel)
                c.id > first_answer_time and  # Created after initial answer
                c.called_user and  # Has a target user (not system process)
                c.called_user != self.answered_user  # Different from original answerer
            )
        ).sorted('id')
        
        logger.info(f"Found {len(potential_transfer_channels)} potential transfer channels")
        
        for channel in potential_transfer_channels:
            time_since_answer = (channel.create_date - first_answer_channel.create_date).total_seconds()
            
            # Apply heuristics to determine if this is really a transfer
            is_likely_transfer = False
            reasons = []
            
            # Heuristic 1: Created well after answer (not simultaneous)
            if time_since_answer > simultaneous_threshold:
                is_likely_transfer = True
                reasons.append(f"created {time_since_answer:.1f}s after answer")
            
            # Heuristic 2: Check if there's a gap in channel creation (suggests user action)
            prev_channels = self.channels.filtered(lambda c: c.id < channel.id).sorted('id', reverse=True)
            if prev_channels:
                last_channel = prev_channels[0]
                gap_time = (channel.create_date - last_channel.create_date).total_seconds()
                if gap_time > 2:  # 2+ second gap suggests user-initiated action
                    is_likely_transfer = True
                    reasons.append(f"gap of {gap_time:.1f}s since last channel")
            
            # Heuristic 3: Different technical_direction pattern
            if channel.technical_direction == 'outbound-dial' and channel.parent_channel.technical_direction == 'inbound':
                reasons.append("outbound-dial from inbound parent")
            
            logger.info(f"  Channel {channel.id} -> {channel.called_user.login}:")
            logger.info(f"    is_likely_transfer={is_likely_transfer}")
            logger.info(f"    reasons: {', '.join(reasons) if reasons else 'none'}")
            logger.info(f"    time_since_answer={time_since_answer:.1f}s")
            
            if is_likely_transfer:
                user_id = channel.called_user.id
                if user_id not in transferred_user_ids:
                    transferred_user_ids.append(user_id)
                    logger.info(f"    -> ADDED to transferred_users: {channel.called_user.login}")
            else:
                logger.info(f"    -> REJECTED as transfer (likely simultaneous ring)")
        
        # Update the field
        if transferred_user_ids:
            new_transferred_users = self.env['res.users'].browse(transferred_user_ids)
            self.transferred_users = [(6, 0, transferred_user_ids)]
            logger.info(f"Call {self.id} transferred_users updated to: {[u.login for u in new_transferred_users]}")
        else:
            self.transferred_users = [(5, 0, 0)]
            logger.info(f"Call {self.id} no valid transfers found, transferred_users cleared")
        
        logger.info(f"=== END ENHANCED TRANSFER DEBUG FOR CALL {self.id} ===")

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
            
        # Update call status and duration based on all channels
        if channel.call:
            # Always update call status when any channel status changes
            channel.call._update_call_status_from_channels()
            
            # Set call duration as sum of all channel durations
            if channel.call.channels:
                total_duration = sum(channel.call.channels.mapped('duration') or [0])
                channel.call.duration = total_duration
                logger.debug(f"Call {channel.call.id} total duration updated to {total_duration} seconds from {len(channel.call.channels)} channels")
            
            # UPDATE TRANSFERRED USERS - Add this new line
            channel.call._update_transferred_users_from_channels()
            
        # REMOVE THE OLD ANSWERED USER LOGIC - now handled by _update_call_status_from_channels()
        
        if (channel.call.direction == 'incoming' and params.get('CallStatus') == 'initiated' and
                params.get('To').startswith('sip:')):
            # Desktop notification only for SIP calls.
            channel.connect_notify()
        # Register call when the last channel closes.
        latest_channel = channel.call.channels.sorted(key='id', reverse=True)[0]
        if channel == latest_channel and params.get('CallStatus') in CALL_END_STATUSES:
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
        return '<Response><Hangup/></Response>'

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
                for user in channel.call.called_users:
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
