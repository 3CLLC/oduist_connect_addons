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
        Update call status based on real channel pattern analysis.
        
        Key insights from debug data:
        1. Root channel = incoming call, always goes to voicemail (completed but no called_pbx_user)
        2. Child channels = actual user interactions (parallel rings + transfers)
        3. Human interaction only happens in child channels with called_pbx_user
        4. Sequential child creation suggests transfers, parallel creation suggests ring group
        """
        self.ensure_one()
        
        if not self.channels:
            logger.warning(f"Call {self.id} has no channels to determine status from")
            return
        
        # Separate channels by type
        root_channels = self.channels.filtered(lambda c: not c.parent_channel)
        child_channels = self.channels.filtered(lambda c: c.parent_channel)
        
        logger.debug(f"Call {self.id}: {len(root_channels)} root, {len(child_channels)} child channels")
        
        if not child_channels:
            # No child channels = no user interaction attempted, use root status
            # This would be very unusual based on our debug data
            new_status = root_channels[0].status if root_channels else 'no-answer'
            logger.debug(f"Call {self.id} no child channels - using root status: {new_status}")
        else:
            # Analyze child channels to determine actual call outcome
            new_status = self._analyze_child_channel_interactions(child_channels)
            logger.debug(f"Call {self.id} determined from child channels: {new_status}")
        
        # Only update if status actually changed
        if self.status != new_status:
            logger.info(f"Updating call {self.id} status from '{self.status}' to '{new_status}'")
            self.status = new_status
            
            # Update answered user for completed calls
            if new_status == 'completed':
                self._update_answered_user_from_channels()
        else:
            logger.debug(f"Call {self.id} status remains '{self.status}'")

    def _analyze_child_channel_interactions(self, child_channels):
        """
        DEBUG VERSION: Analyze child channels to determine the true call outcome.
        Child channels represent the actual user interactions.
        """
        logger.info(f"=== ANALYZING CHILD CHANNELS FOR CALL {self.id} ===")
        logger.info(f"Total child channels: {len(child_channels)}")
        
        # Find channels where humans actually answered
        human_answered = child_channels.filtered(
            lambda c: c.status == 'completed' and c.called_pbx_user
        )
        
        logger.info(f"Human answered channels: {len(human_answered)} (IDs: {human_answered.mapped('id')})")
        
        for channel in human_answered:
            logger.info(f"  Human channel {channel.id}: status={channel.status}, pbx_user={channel.called_pbx_user.name}")
        
        if not human_answered:
            # No human answered any child channel
            logger.info("No human answered - checking other statuses")
            channel_statuses = child_channels.mapped('status')
            logger.info(f"Child channel statuses: {channel_statuses}")
            
            if 'failed' in channel_statuses:
                logger.info("Returning 'failed'")
                return 'failed'
            elif 'busy' in channel_statuses:
                logger.info("Returning 'busy'")
                return 'busy'
            else:
                # All were no-answer or other non-completion status
                logger.info("Returning 'no-answer' (default)")
                return 'no-answer'
        
        # At least one human answered - now determine if this was the final outcome
        logger.info(f"At least one human answered - analyzing pattern")
        
        # Check for transfer pattern: multiple human interactions suggesting A->B transfer
        if len(human_answered) > 1:
            # Multiple humans were involved - this suggests transfers
            # The final outcome is determined by the last human interaction
            final_human_channel = human_answered.sorted(key='id', reverse=True)[0]
            logger.info(f"Multiple human interactions - final: channel {final_human_channel.id}, returning 'completed'")
            return 'completed'
        
        # Single human answered - check if there were subsequent transfer attempts
        human_channel = human_answered[0]
        logger.info(f"Single human answered: channel {human_channel.id}")
        
        # Find any channels created AFTER the human answered (potential transfers)
        subsequent_channels = child_channels.filtered(
            lambda c: c.id > human_channel.id
        )
        
        logger.info(f"Subsequent channels after human answer: {len(subsequent_channels)} (IDs: {subsequent_channels.mapped('id')})")
        
        if not subsequent_channels:
            # No transfers after human answered - straightforward completion
            logger.info("No subsequent channels - straightforward completion, returning 'completed'")
            return 'completed'
        
        # There were subsequent channels (transfers) - check their outcome
        subsequent_human_answered = subsequent_channels.filtered(
            lambda c: c.status == 'completed' and c.called_pbx_user
        )
        
        logger.info(f"Subsequent human answered channels: {len(subsequent_human_answered)}")
        
        if subsequent_human_answered:
            # Transfer was successful (someone else answered)
            logger.info("Transfer was successful, returning 'completed'")
            return 'completed'
        else:
            # Transfer was unsuccessful (all subsequent channels failed/no-answer)
            # Based on business requirements, this should be 'no-answer'
            logger.info(f"Transfer unsuccessful - initial answer by channel {human_channel.id} "
                    f"but {len(subsequent_channels)} subsequent channels failed, returning 'no-answer'")
            return 'no-answer'

    def _update_answered_user_from_channels(self):
        """
        Set the answered user based on the final/last channel that completed.
        For transfers, this should be the user who ultimately handled the call.
        """
        self.ensure_one()
        
        # Find the last completed channel (by ID, which represents chronological order)
        completed_channels = self.channels.filtered(lambda c: c.status == 'completed')
        if not completed_channels:
            logger.warning(f"Call {self.id} marked as completed but no completed channels found")
            return
        
        # Get the last (newest) completed channel
        final_channel = completed_channels.sorted(key='id', reverse=True)[0]
        logger.debug(f"Call {self.id} final completed channel: {final_channel.id}")
        
        # Set answered PBX user from the final channel
        if final_channel.called_pbx_user:
            self.answered_pbx_user = final_channel.called_pbx_user
            # Set answered Odoo user if PBX user has associated Odoo user
            if final_channel.called_pbx_user.user:
                self.answered_user = final_channel.called_pbx_user.user
                logger.debug(f"Call {self.id} answered by user: {self.answered_user.login}")
        else:
            logger.warning(f"Final channel {final_channel.id} has no called_pbx_user")

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
