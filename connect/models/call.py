# -*- coding: utf-8 -*-

import json
import logging
import re
from urllib.parse import urljoin
from markupsafe import Markup
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
    # Temporary transfer context for webhook processing (cleared after use)
    transfer_context = fields.Json(string='Transfer Context', readonly=True, help='Temporary storage for transfer targets during webhook processing')
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
                # Check if this is a missed call for notification purposes
                is_missed_call = (
                    # Regular missed call: incoming, nobody answered
                    (rec.direction == 'incoming' and 
                     rec.status in ['no-answer', 'busy', 'failed'] and 
                     not rec.answered_user) 
                    or
                    # Missed transfer: transfer occurred but nobody completed it
                    (rec.transferred_users and not rec.completed_by_user)
                )
                
                if is_missed_call:
                    # Use missed call format for notification titles
                    caller_name = None
                    caller_number = rec.caller
                    
                    # Try to get contact name from partner
                    if rec.partner:
                        caller_name = rec.partner.name
                    elif rec.caller_user:
                        caller_name = rec.caller_user.name
                    
                    # Format the caller display
                    if caller_name and caller_number:
                        caller_display = f"{caller_name} ({caller_number})"
                    elif caller_name:
                        caller_display = caller_name
                    elif caller_number:
                        caller_display = caller_number
                    else:
                        caller_display = "Unknown"
                    
                    rec.name = f"Missed call from {caller_display}"
                else:
                    # Use standard format with Eastern Time for regular calls
                    call_datetime = rec.create_date
                    try:
                        import pytz
                        eastern = pytz.timezone('US/Eastern')
                        utc_datetime = pytz.utc.localize(call_datetime)
                        eastern_datetime = utc_datetime.astimezone(eastern)
                        
                        # Determine if EST or EDT
                        timezone_name = eastern_datetime.strftime('%Z')  # EST or EDT
                        formatted_date = eastern_datetime.strftime(f'%B %d, %Y at %I:%M %p {timezone_name}')
                    except:
                        # Fallback if timezone conversion fails
                        formatted_date = call_datetime.strftime('%B %d, %Y at %I:%M %p EST')
                    
                    rec.name = '{} {} call {}'.format(rec.status, rec.direction, formatted_date).capitalize()
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
        
        # Outgoing calls are always marked as completed
        if self.direction == 'outgoing':
            self.status = 'completed'
            logger.info(f"Call {self.id}: Status set to 'completed' (outgoing call)")
        elif self.answered_user:
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
        For outgoing calls, handle differently since caller (not called) is the internal user.
        """
        self.ensure_one()
        logger.info(f"Call {self.id}: Populating user fields for direct call pattern")
        
        # Handle outgoing calls differently
        if self.direction == 'outgoing':
            self._populate_outgoing_call_user_fields()
            return
        
        # Find all channels with users (completed or not) for incoming calls
        user_channels = self.channels.filtered(lambda c: c.called_pbx_user and c.called_pbx_user.user)
        
        if not user_channels:
            logger.warning(f"Call {self.id}: No channels with users found for direct call")
            return
            
        # Sort by creation order to determine call flow
        user_channels_by_time = user_channels.sorted('create_date')
        
        # ANSWERED USER: Only set if someone actually answered (has completed channel)
        # Use same completion-based logic as ring groups
        completed_channels = user_channels.filtered(lambda c: c.status == 'completed')
        
        # Filter out transfer recipients from initial answer detection
        # The person who answered initially should not be a transfer recipient
        if self.transferred_users and completed_channels:
            initial_answered_channels = completed_channels.filtered(
                lambda c: c.called_pbx_user.user not in self.transferred_users
            )
            if initial_answered_channels:
                # Use same logic as ring groups - earliest ID if multiple, otherwise just take it
                if len(initial_answered_channels) > 1:
                    answered_channel = initial_answered_channels.sorted('id')[0]
                    logger.warning(f"Call {self.id}: Multiple initial completed channels, using earliest: {answered_channel.id}")
                else:
                    answered_channel = initial_answered_channels[0]
                    
                self.answered_user = answered_channel.called_pbx_user.user
                self.answered_pbx_user = answered_channel.called_pbx_user
                logger.info(f"Call {self.id}: answered_user set to {self.answered_user.login} (initial answerer, excluding transfers)")
            else:
                # All completed channels are transfer recipients - no initial answerer
                logger.info(f"Call {self.id}: No initial answerer found (all completed channels are transfers)")
        elif completed_channels:
            # No transfers, use same logic as ring groups
            if len(completed_channels) > 1:
                answered_channel = completed_channels.sorted('id')[0]
                logger.warning(f"Call {self.id}: Multiple completed channels, using earliest: {answered_channel.id}")
            else:
                answered_channel = completed_channels[0]
                
            self.answered_user = answered_channel.called_pbx_user.user
            self.answered_pbx_user = answered_channel.called_pbx_user
            logger.info(f"Call {self.id}: answered_user set to {self.answered_user.login} (completed channel, no transfers)")
        else:
            # No completed channels - no one answered
            logger.info(f"Call {self.id}: No completed channels found - leaving answered_user empty")
        
        # COMPLETED BY USER: User who actually completed the call
        if self.transferred_users:
            # Transfer occurred - check if completed_by_user was already set by extension handler
            if not self.completed_by_user:
                # Extension handler hasn't set completion yet - check for completed transfer channels
                # Re-query completed channels to ensure we have any newly created transfer channels
                all_user_channels = self.channels.filtered(lambda c: c.called_pbx_user and c.called_pbx_user.user)
                completed_channels = all_user_channels.filtered(lambda c: c.status == 'completed')
                
                transfer_completed_channels = completed_channels.filtered(
                    lambda c: c.called_pbx_user.user in self.transferred_users
                )
                if transfer_completed_channels:
                    # Transfer recipient completed the call
                    if len(transfer_completed_channels) > 1:
                        final_channel = transfer_completed_channels.sorted('id')[-1]
                        logger.info(f"Call {self.id}: Multiple transfer completions, using latest: {final_channel.id}")
                    else:
                        final_channel = transfer_completed_channels[0]
                    
                    self.completed_by_user = final_channel.called_pbx_user.user
                    logger.info(f"Call {self.id}: completed_by_user set to transfer recipient {self.completed_by_user.login} (from channel)")
                else:
                    # Transfer failed, nobody completed the call
                    logger.info(f"Call {self.id}: Transfer failed - completed_by_user left empty for missed call notifications")
            else:
                logger.info(f"Call {self.id}: completed_by_user already set by extension handler: {self.completed_by_user.login}")
        else:
            # No transfer - original answerer completed
            self.completed_by_user = self.answered_user
            logger.info(f"Call {self.id}: completed_by_user set to original answerer {self.completed_by_user.login} (no transfer)")

    def _populate_outgoing_call_user_fields(self):
        """
        Populate user fields for outgoing calls (internal user calling external party).
        called_users: External recipient (original target of outgoing call)
        answered_user: External recipient (if they answered) - Odoo contact or phone number
        completed_by_user: Last internal person who handled the call (caller or transfer recipient)
        """
        self.ensure_one()
        logger.info(f"Call {self.id}: Populating user fields for outgoing call")
        
        # Find the outbound-dial channel (represents the external party)
        outbound_channel = None
        for channel in self.channels:
            if channel.technical_direction == 'outbound-dial':
                outbound_channel = channel
                break
        
        if outbound_channel:
            # CALLED USERS: Set to external recipient (original target of outgoing call)
            external_number = outbound_channel.called_number
            if outbound_channel.partner and outbound_channel.partner.user_id:
                # External party has an Odoo user account - use that
                self.called_users = [(4, outbound_channel.partner.user_id.id)]
                logger.info(f"Call {self.id}: called_users set to Odoo contact {outbound_channel.partner.name}")
            else:
                # No Odoo user for external party - clear any transfer recipients that may have been added
                # The phone number is tracked in the 'called' field
                self.called_users = [(5,)]  # Clear all called_users
                logger.info(f"Call {self.id}: External party {external_number} has no Odoo user - called_users cleared of transfer recipients")
            
            # ANSWERED USER: Set to external recipient only if they actually answered
            external_answered = (outbound_channel.status in ['in-progress', 'completed'] and 
                               outbound_channel.duration and outbound_channel.duration > 0)
            
            if external_answered:
                # External party answered - set answered_user to same as called_users
                if outbound_channel.partner and outbound_channel.partner.user_id:
                    self.answered_user = outbound_channel.partner.user_id
                    logger.info(f"Call {self.id}: answered_user set to Odoo contact {outbound_channel.partner.name}")
                else:
                    # No Odoo contact found - would need to create a user record for phone number
                    # For now, leave empty and log the external number
                    logger.info(f"Call {self.id}: External party {external_number} answered, but no Odoo contact found")
            else:
                # External party didn't answer (voicemail, busy, no-answer)
                logger.info(f"Call {self.id}: External party didn't answer (status: {outbound_channel.status})")
        
        # COMPLETED BY USER: Last internal person who handled the call
        # Check for transfer recipients first (they're the ones who completed it)
        internal_channels = self.channels.filtered(lambda c: c.called_pbx_user and c.called_pbx_user.user)
        
        if internal_channels:
            # There are transfer recipients - find who completed the call
            completed_internal = internal_channels.filtered(lambda c: c.status == 'completed')
            if completed_internal:
                # Someone completed via transfer
                if len(completed_internal) > 1:
                    completed_channel = completed_internal.sorted('id')[-1]  # Most recent
                    logger.warning(f"Call {self.id}: Multiple completed transfer channels, using latest: {completed_channel.id}")
                else:
                    completed_channel = completed_internal[0]
                
                self.completed_by_user = completed_channel.called_pbx_user.user
                logger.info(f"Call {self.id}: completed_by_user set to transfer recipient {self.completed_by_user.login}")
            else:
                # Transfer channels exist but none completed - no one handled the call
                # Leave completed_by_user empty since transfer failed and no one answered
                logger.info(f"Call {self.id}: Transfer attempted but no transfer recipient completed - completed_by_user remains empty")
        else:
            # No transfer recipients - original caller handled the call
            self._set_original_caller_as_completer()
    
    def _set_original_caller_as_completer(self):
        """Helper to set original caller as completed_by_user for outgoing calls"""
        caller_channel = None
        for channel in self.channels:
            if channel.caller_pbx_user and channel.caller_pbx_user.user:
                caller_channel = channel
                break
        
        if caller_channel:
            self.completed_by_user = caller_channel.caller_pbx_user.user
            logger.info(f"Call {self.id}: completed_by_user set to original caller {self.completed_by_user.login}")
        else:
            logger.warning(f"Call {self.id}: Could not identify original caller for outgoing call")

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
        # Exclude channels that were updated by transfer completion (they have transferred users)
        completed_ring_channels = ring_group_channels.filtered(lambda c: c.status == 'completed')
        
        if not completed_ring_channels:
            logger.info(f"Call {self.id}: No completed ring_group channels found - no one answered")
            return
        
        # Filter out channels that belong to transfer recipients (they shouldn't count as ring group answers)
        if self.transferred_users:
            genuine_ring_answered = completed_ring_channels.filtered(
                lambda c: c.called_pbx_user.user not in self.transferred_users
            )
            if genuine_ring_answered:
                filtered_count = len(completed_ring_channels) - len(genuine_ring_answered)
                completed_ring_channels = genuine_ring_answered
                logger.info(f"Call {self.id}: Filtered out {filtered_count} transfer recipient channels from ring group answers")
            
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
            # Transfer occurred - check if completed_by_user was already set by extension handler
            if not self.completed_by_user:
                # Extension handler hasn't set completion yet - check for completed transfer channels
                transfer_completed_channels = []
                for user in self.transferred_users:
                    user_channels = self.channels.filtered(lambda c: c.called_user and c.called_user.id == user.id and c.status == 'completed')
                    transfer_completed_channels.extend(user_channels)
                
                if transfer_completed_channels:
                    # Use the most recent completed transfer channel
                    latest_channel = sorted(transfer_completed_channels, key=lambda c: c.id)[-1]
                    self.completed_by_user = latest_channel.called_user
                    logger.info(f"Call {self.id}: completed_by_user set to transfer recipient {self.completed_by_user.login} (from channel)")
                else:
                    # No completed transfer channels - transfer failed, leave empty for missed call notifications
                    logger.info(f"Call {self.id}: Transfer failed - completed_by_user left empty for missed call notifications")
            else:
                logger.info(f"Call {self.id}: completed_by_user already set by extension handler: {self.completed_by_user.login}")
        else:
            # No transfer - answered user also completed
            self.completed_by_user = self.answered_user
            logger.info(f"Call {self.id}: completed_by_user set to answerer {self.answered_user.login} (no transfer)")

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
                
                # Set webhook expectation for transfer channel
                self._set_webhook_expectation('transfer', {
                    'expected_count': 1,
                    'received_count': 0,
                    'target_user_id': user.id,
                    'target_user_login': user.login
                })
                
                logger.info(f"Call {self.id}: Transfer initiated to {user.login} (added to transferred_users) - expecting 1 transfer channel")
                
                # Update call pattern if needed - transfers can help us understand the call type
                if not self.call_pattern:
                    detected_pattern = self._detect_call_pattern()
                    if detected_pattern:
                        self.call_pattern = detected_pattern
                        logger.info(f"Call {self.id}: Pattern detection triggered by transfer: '{detected_pattern}'")

    def store_transfer_context(self, dial_call_sid, target_user):
        """
        Store temporary transfer context for webhook processing.
        Maps DialCallSid to target user for reliable webhook processing.
        """
        self.ensure_one()
        if not dial_call_sid or not target_user:
            return
            
        current_context = self.transfer_context or {}
        current_context[dial_call_sid] = {
            'user_id': target_user.id,
            'user_login': target_user.login
        }
        self.transfer_context = current_context
        logger.info(f"Call {self.id}: Stored transfer context for {dial_call_sid} -> {target_user.login}")

    def get_transfer_target(self, dial_call_sid):
        """
        Get transfer target from temporary context storage.
        Returns user record or None if not found.
        """
        self.ensure_one()
        if not self.transfer_context or not dial_call_sid:
            return None
            
        context_data = self.transfer_context.get(dial_call_sid)
        if context_data and 'user_id' in context_data:
            user = self.env['res.users'].sudo().browse(context_data['user_id'])
            if user.exists():
                logger.info(f"Call {self.id}: Retrieved transfer target from context: {user.login}")
                return user
        return None

    def store_external_call_leg(self, external_call_sid):
        """
        Store external call leg SID for outgoing call transfers.
        This provides quick access during transfers without database searches.
        """
        self.ensure_one()
        if not external_call_sid:
            return
            
        current_context = self.transfer_context or {}
        current_context['_external_leg'] = external_call_sid
        self.transfer_context = current_context
        logger.info(f"Call {self.id}: Stored external call leg SID: {external_call_sid}")

    def get_external_call_leg(self):
        """
        Get external call leg SID for outgoing call transfers.
        Returns SID string or None if not found.
        """
        self.ensure_one()
        if not self.transfer_context:
            return None
            
        external_leg = self.transfer_context.get('_external_leg')
        if external_leg:
            logger.info(f"Call {self.id}: Retrieved external call leg SID: {external_leg}")
            return external_leg
        return None

    def clear_transfer_context(self):
        """
        Clear temporary transfer context after call processing is complete.
        """
        self.ensure_one()
        if self.transfer_context:
            logger.info(f"Call {self.id}: Clearing transfer context")
            self.transfer_context = None
        # Note: We don't clear transfer_completion_handled here as it's permanent state for the call

    def _set_webhook_expectation(self, source, data):
        """Set expectation for incoming webhook data"""
        from odoo import fields
        import datetime
        
        current_context = self.transfer_context or {}
        if 'webhook_expectations' not in current_context:
            current_context['webhook_expectations'] = {}
        
        current_context['webhook_expectations'][source] = {
            'timestamp': fields.Datetime.now().isoformat(),
            'expected_count': data.get('expected_count', 1),
            'received_count': data.get('received_count', 0),
            **data  # Include any additional data
        }
        
        self.transfer_context = current_context
        logger.info(f"Call {self.id}: Set {source} webhook expectation - expecting {data.get('expected_count', 1)} channels")

    def _increment_webhook_expectation(self, source):
        """Increment received count for webhook expectation and clear if complete"""
        if not self.transfer_context:
            return
            
        context = self.transfer_context
        expectations = context.get('webhook_expectations', {})
        
        if source not in expectations:
            return
            
        expectations[source]['received_count'] += 1
        received = expectations[source]['received_count']
        expected = expectations[source]['expected_count']
        
        logger.info(f"Call {self.id}: {source} expectation progress: {received}/{expected}")
        
        if received >= expected:
            logger.info(f"Call {self.id}: {source} expectation fulfilled - clearing")
            del expectations[source]
        
        context['webhook_expectations'] = expectations
        self.transfer_context = context

    def _has_pending_webhooks(self):
        """Check if we're still expecting webhook data"""
        if not self.transfer_context:
            return False
        
        expectations = self.transfer_context.get('webhook_expectations', {})
        if not expectations:
            return False
        
        # Check for timeout (15 seconds)
        from odoo import fields
        import datetime
        cutoff = fields.Datetime.now() - datetime.timedelta(seconds=15)
        
        for source, data in expectations.items():
            timestamp = fields.Datetime.from_string(data['timestamp'])
            if timestamp > cutoff:
                return True  # Still within timeout window
        
        # All expectations have timed out
        logger.warning(f"Call {self.id}: Webhook expectations timed out, proceeding with finalization")
        return False

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
            # Set call pattern for outgoing calls (always direct_call since they're one-to-one)
            call_pattern = 'direct_call' if direction == 'outgoing' else False
            
            call = self.with_context(tracking_disable=True).create({
                'partner': channel.partner.id,
                'called': channel.called_number,
                'caller': channel.caller_number,
                'status': channel.status,
                'caller_pbx_user': channel.caller_pbx_user.id,
                'caller_user': channel.caller_user.id,
                'direction': direction,
                'call_pattern': call_pattern,
            })
            channel.call = call
        elif channel.parent_channel and channel.parent_channel.call:
            # Secondary channel, assign the call from the parent.
            channel.call = channel.parent_channel.call
            # Only set to internal for true internal calls, not outgoing calls with transfers
            if channel.call.direction != 'outgoing':
                if channel.caller_pbx_user and channel.parent_channel.called_pbx_user:
                    channel.call.direction = 'internal'
                elif channel.called_pbx_user and channel.parent_channel.caller_pbx_user:
                    channel.call.direction = 'internal'
                
        # Set called from 2nd call leg for click2call external calls.
        if channel.parent_channel and channel.parent_channel.technical_direction == 'outbound-api':
            channel.call.called = channel.called_number
        # Set called users - only for originally called users, not transfer recipients
        if channel.called_user:
            # Use call_source to distinguish between original calls and transfers
            if hasattr(channel, 'call_source') and channel.call_source == 'transfer':
                # Increment transfer webhook expectation when transfer channel is created
                channel.call._increment_webhook_expectation('transfer')
                logger.info(f"Skipped adding {channel.called_user.login} to called_users - call_source indicates this is a transfer recipient")
            else:
                # This is an originally called user (direct_call, ring_group, or no call_source yet)
                channel.call.called_users = [(4, channel.called_user.id)]
                
                # Increment webhook expectation if this is a ring group channel
                if hasattr(channel, 'call_source') and channel.call_source == 'ring_group':
                    channel.call._increment_webhook_expectation('ring_group')
                
                logger.info(f"Added {channel.called_user.login} to called_users - originally called user (call_source: {getattr(channel, 'call_source', 'None')}) for call {channel.call.id}")
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
        # Register call only when ALL channels have ended AND no pending webhook expectations
        # Check if this channel ending means the entire call is complete
        all_channels_ended = all(ch.status in CALL_END_STATUSES for ch in channel.call.channels)
        has_pending_webhooks = channel.call._has_pending_webhooks()
        
        if (all_channels_ended and 
            params.get('CallStatus') in CALL_END_STATUSES and 
            not has_pending_webhooks):
            # NOW do all the final call processing
            logger.info(f"Call {channel.call.id}: All conditions met for finalization - no pending webhook expectations")
            channel.call._finalize_call_details()
            self.register_call(channel, params)
        else:
            if not all_channels_ended:
                reason = "channels still active"
            elif has_pending_webhooks:
                reason = "pending webhook expectations"
            else:
                reason = "channel not ending"
            logger.info(f"Call {channel.call.id}: Finalization deferred - {reason}")
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
        
        # SIMULTANEOUS_RINGS: For ring groups, find existing recipient channel
        # Ring groups create channels for all users upfront, so recipient channel should exist
        recipient_channel = None
        if call.call_pattern == 'ring_group':
            child_channels = call.channels.filtered(lambda c: c.parent_channel)
            if child_channels:
                # Sort by ID (creation order) and look for the most recent one that's not completed
                potential_recipients = child_channels.filtered(lambda c: c.status in ['no-answer', 'ringing', 'in-progress'])
                if potential_recipients:
                    recipient_channel = potential_recipients.sorted('id', reverse=True)[0]
                    logger.info(f"SIMULTANEOUS_RINGS: Using existing channel {recipient_channel.id} as recipient")
                else:
                    logger.error(f"SIMULTANEOUS_RINGS: No suitable recipient channels found for ring group call {call.id}")
                    return
            else:
                logger.error(f"SIMULTANEOUS_RINGS: No child channels found for ring group call {call.id}")
                return
        
        # DIRECT_CALLS: For direct calls, create missing transfer channel 
        # Direct calls don't create channels for transfer targets, so we need to create them
        elif call.call_pattern == 'direct_call':
            logger.info(f"DIRECT_CALLS: Creating transfer channel for direct call transfer")
            recipient_channel = self._create_missing_transfer_channel(call, dial_call_sid, dial_status, params)
            if recipient_channel:
                logger.info(f"DIRECT_CALLS: Created missing transfer channel {recipient_channel.id}")
            else:
                logger.error(f"DIRECT_CALLS: Could not create missing transfer channel for call {call.id}")
                return
        
        # UNKNOWN_PATTERN: Fail explicitly for unknown call patterns
        else:
            logger.error(f"UNKNOWN_PATTERN: Cannot process transfer completion for call {call.id} with unknown pattern '{call.call_pattern}'. Transfer processing aborted to prevent incorrect field population.")
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

    def _create_missing_transfer_channel(self, call, dial_call_sid, dial_status, params):
        """
        Create a missing transfer channel for DIRECT_CALLS when no existing recipient channel found.
        This method uses multiple strategies to identify the transfer target without relying on 
        transferred_users field due to transaction timing issues.
        """
        try:
            logger.info(f"=== CREATING MISSING TRANSFER CHANNEL ===")
            logger.info(f"Call ID: {call.id}, DialCallSid: {dial_call_sid}")
            
            # Find the parent channel for the transfer
            parent_channel = call.channels.filtered(lambda c: not c.parent_channel)
            if not parent_channel:
                logger.warning(f"No parent channel found for call {call.id}")
                return None
            parent_channel = parent_channel[0]
            
            # Try multiple strategies to determine transfer target:
            target_user = None
            
            # PRIMARY: Check transfer context (temporary storage for webhook processing)
            target_user = call.get_transfer_target(dial_call_sid)
            if not target_user:
                # Try using the original CallSid (parent call) as fallback
                original_call_sid = params.get('CallSid')  # This is the main call SID
                if original_call_sid:
                    target_user = call.get_transfer_target(original_call_sid)
            if target_user:
                logger.info(f"Using transfer context target: {target_user.login}")
            
            # FALLBACK: Check current call's transferred_users (set during transfer initiation)
            if not target_user:
                call_with_sudo = call.sudo()  # Ensure we can read the field
                if call_with_sudo.transferred_users:
                    target_user = call_with_sudo.transferred_users[-1]  # Most recent transfer target
                    logger.info(f"Using current call transfer target: {target_user.login}")
            
            # If we still can't determine the target, fail explicitly
            if not target_user:
                logger.error(f"Cannot determine transfer target for call {call.id} - no transfer context or transferred_users available")
                return None
                
            # Find the PBX user for this Odoo user
            pbx_user = self.env['connect.user'].sudo().search([('user', '=', target_user.id)], limit=1)
            if not pbx_user:
                logger.warning(f"Could not find PBX user for {target_user.login}")
                return None
            
            # Create the missing transfer channel
            channel_data = {
                'sid': dial_call_sid,
                'call': call.id,
                'parent_channel': parent_channel.id,
                'technical_direction': 'outbound-dial',
                'status': dial_status,
                'duration': int(params.get('DialCallDuration', 0)),
                'called_pbx_user': pbx_user.id,
                'called_user': target_user.id,
                'call_source': 'transfer',  # Explicitly tag as transfer
                'caller': parent_channel.caller,
                'called': pbx_user.uri
            }
            
            logger.info(f"Creating transfer channel with data: {channel_data}")
            recipient_channel = self.env['connect.channel'].create(channel_data)
            logger.info(f"SUCCESS: Created missing transfer channel {recipient_channel.id} for {target_user.login}")
            return recipient_channel
            
        except Exception as e:
            logger.error(f"Failed to create missing transfer channel: {e}", exc_info=True)
            return None

    def _format_missed_call_message(self, channel):
        """
        Create a clean, professional missed call message format.
        Format: "Missed call from <name> (<number>)\n<month> <day>, <year> at <time> EST/EDT"
        """
        # Get caller information
        caller_name = None
        caller_number = None
        
        if channel.call.direction == 'incoming':
            caller_number = channel.call.caller
            # Try to get contact name from partner
            if channel.call.partner:
                caller_name = channel.call.partner.name
            elif channel.call.caller_user:
                caller_name = channel.call.caller_user.name
        else:  # outgoing call
            caller_number = channel.call.called
            # For outgoing calls, the "caller" from user perspective is who they called
            if channel.call.partner:
                caller_name = channel.call.partner.name
        
        # Format the caller display
        if caller_name and caller_number:
            caller_display = f"{caller_name} ({caller_number})"
        elif caller_name:
            caller_display = caller_name
        elif caller_number:
            caller_display = caller_number
        else:
            caller_display = "Unknown"

        # Build link to call details
        call_link = f" <a href='/web#id={channel.call.id}&model=connect.call&view_type=form'>Click to view the call details</a>."

        # Add transfer context
        transfer_info = ""
        if channel.call.answered_user:
            transfer_info = f" Call transferred to you by {channel.call.answered_user.name}."
        
        # Build body with call details link
        body = Markup(f"You missed a call from {caller_display}.{transfer_info}{call_link}")

        subject = f"Missed call from {caller_display}"

        return subject, body
    
    # def _format_missed_transfer_message(self, channel):
    #     """
    #     Create a clean missed transfer message format.
    #     """
    #     # Get original caller information
    #     caller_name = None
    #     caller_number = None
        
    #     if channel.call.direction == 'incoming':
    #         caller_number = channel.call.caller
    #         if channel.call.partner:
    #             caller_name = channel.call.partner.name
    #         elif channel.call.caller_user:
    #             caller_name = channel.call.caller_user.name
    #     else:
    #         caller_number = channel.call.called
    #         if channel.call.partner:
    #             caller_name = channel.call.partner.name
        
    #     # Format caller display
    #     if caller_name and caller_number:
    #         caller_display = f"{caller_name} ({caller_number})"
    #     elif caller_name:
    #         caller_display = caller_name
    #     elif caller_number:
    #         caller_display = caller_number
    #     else:
    #         caller_display = "Unknown"
        
    #     # Convert to Eastern Time
    #     call_datetime = channel.create_date
    #     try:
    #         import pytz
    #         eastern = pytz.timezone('US/Eastern')
    #         utc_datetime = pytz.utc.localize(call_datetime)
    #         eastern_datetime = utc_datetime.astimezone(eastern)
            
    #         timezone_name = eastern_datetime.strftime('%Z')
    #         formatted_date = eastern_datetime.strftime(f'%B %d, %Y at %I:%M %p {timezone_name}')
    #     except:
    #         formatted_date = call_datetime.strftime('%B %d, %Y at %I:%M %p EST')
        


    #     subject = f"Missed Call from {caller_display}"
    #     content = f""
        
    #     return f"Missed transfer from {caller_display}{transfer_info}<br>{formatted_date}"

    def register_call(self, channel, params):
        try:
            notify_users = []
            
            # COMPREHENSIVE DEBUGGING: Log all user field states
            logger.info(f"=== REGISTER_CALL DEBUG START - Call {channel.call.id} ===")
            logger.info(f"Call direction: {channel.call.direction}")
            logger.info(f"Call status: {channel.call.status}")
            logger.info(f"Call pattern: {channel.call.call_pattern}")
            logger.info(f"answered_user: {channel.call.answered_user.login if channel.call.answered_user else 'None'}")
            logger.info(f"completed_by_user: {channel.call.completed_by_user.login if channel.call.completed_by_user else 'None'}")
            
            # Log all user lists with details
            called_users_info = []
            for user in channel.call.called_users:
                connect_user = user.connect_user[0] if user.connect_user else None
                missed_notify = connect_user.missed_calls_notify if connect_user else False
                called_users_info.append(f"{user.login}(notify:{missed_notify})")
            logger.info(f"called_users ({len(channel.call.called_users)}): {called_users_info}")
            
            transferred_users_info = []
            for user in channel.call.transferred_users:
                connect_user = user.connect_user[0] if user.connect_user else None
                missed_notify = connect_user.missed_calls_notify if connect_user else False
                transferred_users_info.append(f"{user.login}(notify:{missed_notify})")
            logger.info(f"transferred_users ({len(channel.call.transferred_users)}): {transferred_users_info}")
            
            # Check for overlaps between called_users and transferred_users
            overlap_users = set(channel.call.called_users.ids) & set(channel.call.transferred_users.ids)
            if overlap_users:
                overlap_logins = [u.login for u in channel.call.called_users.filtered(lambda x: x.id in overlap_users)]
                logger.warning(f"OVERLAP DETECTED: Users in both called_users AND transferred_users: {overlap_logins}")
            else:
                logger.info("No overlap between called_users and transferred_users")
            
            # Construct base message from lines
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
            
            # Simplified notification logic based on user field states
            logger.info(f"=== SIMPLIFIED NOTIFICATION LOGIC START ===")
            logger.info(f"Call state - called_users: {len(channel.call.called_users)}, answered_user: {bool(channel.call.answered_user)}, transferred_users: {len(channel.call.transferred_users)}, completed_by_user: {bool(channel.call.completed_by_user)}")
            
            # Rule 1: called_users only (no other fields) → Everyone gets notification
            if (channel.call.called_users and 
                not channel.call.answered_user and 
                not channel.call.transferred_users and 
                not channel.call.completed_by_user):
                
                logger.info(f"RULE 1: called_users only - everyone gets notification")
                for user in channel.call.called_users:
                    connect_user = user.connect_user
                    if connect_user and connect_user[0].missed_calls_notify:
                        notify_users.append(user)
                        logger.info(f"  ✓ ADDED {user.login} to notifications (called user)")
                    else:
                        reason = 'no connect_user' if not connect_user else 'notifications disabled'
                        logger.info(f"  ✗ SKIPPED {user.login} - {reason}")
                        
            # Rule 2: called_users + answered_user + completed_by_user + NO transferred_users → No notifications
            elif (channel.call.called_users and 
                  channel.call.answered_user and 
                  channel.call.completed_by_user and 
                  not channel.call.transferred_users):
                
                logger.info(f"RULE 2: Normal completion (answered + completed, no transfers) - no notifications")
                
            # Rule 3: called_users + answered_user + transferred_users + NO completed_by_user → Only transferred users get notification
            elif (channel.call.called_users and 
                  channel.call.answered_user and 
                  channel.call.transferred_users and 
                  not channel.call.completed_by_user):
                
                logger.info(f"RULE 3: Missed transfer - only transferred users get notifications")
                for user in channel.call.transferred_users:
                    connect_user = user.connect_user
                    if connect_user and connect_user[0].missed_calls_notify:
                        notify_users.append(user)
                        logger.info(f"  ✓ ADDED {user.login} to notifications (missed transfer)")
                    else:
                        reason = 'no connect_user' if not connect_user else 'notifications disabled'
                        logger.info(f"  ✗ SKIPPED {user.login} - {reason}")
                        
            # Rule 4: Any completed_by_user exists → No notifications
            elif channel.call.completed_by_user:
                logger.info(f"RULE 4: Call completed by {channel.call.completed_by_user.login} - no notifications")
                
            else:
                logger.info(f"NO MATCHING RULE: Unhandled call state - no notifications")

            # Register call at partner.
            if channel.call.partner:
                message.insert(3, 'partner: {}, '.format(channel.call.partner.name))
                final_message = ' '.join(message)
                if final_message.endswith(', '):
                    final_message = final_message[:-2] + '.'
                channel.call.register_call_post_message(
                    channel.call.partner, body=final_message, subtype_xmlid='mail.mt_note')

            # Send notifications if any users were identified
            if notify_users:
                logger.info(f"=== SENDING NOTIFICATIONS ===")
                logger.info(f"Sending notifications to {len(notify_users)} users: {[u.login for u in notify_users]}")
                
                # Deduplicate notify_users to prevent multiple notifications to the same user
                original_count = len(notify_users)
                notify_users = list(set(notify_users))
                if len(notify_users) < original_count:
                    logger.warning(f"Removed {original_count - len(notify_users)} duplicate users from notification list")
                
                debug(self, 'Missed call notification to users: {}'.format(notify_users))
                
                # Get formatted notification message
                notify_subject, notify_body = self._format_missed_call_message(channel)

                # Send missed call notifications
                channel.call.register_call_post_message(
                    channel.call,
                    subtype_xmlid='mail.mt_comment',
                    subject=notify_subject,
                    body=notify_body,
                    partner_ids=[k.partner_id.id for k in notify_users]
                )
                logger.info(f"✓ Notifications sent successfully")
            else:
                logger.info("No notifications to send")
                
            logger.info(f"=== REGISTER_CALL DEBUG END - Call {channel.call.id} ===")
            # Clear temporary transfer context after call processing is complete
            channel.call.clear_transfer_context()
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
            "direction",
            "status",
            "answered_user",
            "completed_by_user",
            "transferred_users",
            "call_pattern"
        ]
