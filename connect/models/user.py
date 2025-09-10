# -*- coding: utf-8 -*-

import json
import jinja2
import logging
import re
from urllib.parse import urljoin
from datetime import timedelta
from odoo import fields, models, api, release
from odoo.exceptions import ValidationError
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import Client, Dial, VoiceResponse
from .settings import format_connect_response, debug, strip_number
from .twiml import pretty_xml

logger = logging.getLogger(__name__)


class User(models.Model):
    _name = 'connect.user'
    _rec_name = 'username'
    _description = 'Twilio User'
    _order = 'username'

    sid = fields.Char('SID', readonly=True)
    exten = fields.Many2one('connect.exten', ondelete='set null', readonly=True)
    exten_number = fields.Char(related='exten.number', store=True)
    sip_enabled = fields.Boolean('SIP Phone Enabled')
    client_enabled = fields.Boolean('Web Phone Enabled', default=True)
    name = fields.Char(compute='_get_name')
    user = fields.Many2one('res.users', string='Odoo User', domain=[('share', '=', False)])
    domain = fields.Many2one('connect.domain', required=True, ondelete='cascade',
                            default=lambda x: x.env['connect.domain'].search([], limit=1))
    username = fields.Char(required=True)
    password = fields.Char(groups="connect.group_connect_admin,connect.group_connect_user")
    uri = fields.Char('SIP URI', compute='_get_sip_uri', store=True)
    record_calls = fields.Boolean(default=True)
    voicemail_enabled = fields.Boolean()
    voicemail_prompt = fields.Text(default="Hello, this is {{user.name}}. I'm unable to take your call right now. Please leave a message after the tone.")
    application = fields.Many2one('connect.twiml')
    ring_first = fields.Selection(selection=[('sip', 'SIP'),('client', 'Client')],
                                  required=True, default='client')
    ring_second = fields.Selection(selection=[('sip', 'SIP'),('client', 'Client')],
                                  required=False, default='sip')
    sip_ring_timeout = fields.Integer(required=True, default=30, string='SIP ring timeout')
    client_ring_timeout = fields.Integer(required=True, default=10, string='Web client ring timeout')
    callerid_number = fields.Many2one('connect.number', ondelete='restrict') # TODO: Remove after 1.0
    outgoing_callerid = fields.Many2one('connect.outgoing_callerid', ondelete='set null',
        domain=['|',('status', '=', 'validated'),('callerid_type', '=', 'number')])
    missed_calls_notify = fields.Boolean(default=False, help='Notify user on missed calls.')
    call_popup_is_enabled = fields.Boolean(default=True, string='Enable Call Notifications', help='Enable notifications for call events (transfers, status updates, etc.)')
    call_popup_is_sticky = fields.Boolean(default=False, string='Sticky Call Notifications', help='Require manual dismissal of call notifications?')
    greeting_message = fields.Char()

    _sql_constraints = [
        ('user_uniq', 'UNIQUE("user")', 'This Odoo user account is already defined!'),
        ('username_uniq', 'UNIQUE(username)', 'This PBX username is already defined!'),
    ]

    @api.depends('username', 'domain', 'domain.domain_name', 'domain.subdomain')
    def _get_sip_uri(self):
        for rec in self:
            rec.uri = '{}@{}'.format(rec.username, rec.domain.domain_name)

    def _create_sip_account(self, username, password, client=None):
        self.ensure_one()
        try:
            client = client or self.env['connect.settings'].get_client()
            credential = client.sip.credential_lists(
                self.domain.cred_list_sid).credentials.create(
                    username=username, password=password)
            if not credential:
                raise ValidationError('Cannot create a SIP user!')
            return credential.sid
        except Exception as e:
            if 'A strong password is required' in str(e):
                msg = 'A strong password is required. It must have a minimum length of 12, at least one number, uppercase char and lowercase character.'
                raise ValidationError(msg)
            else:
                raise ValidationError(format_connect_response(e))

    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        if not self.env.context.get('no_twilio_create'):
            for rec in recs:
                try:
                    if rec.sip_enabled and rec.password:
                        if not self.env.context.get('skip_create_credential'):
                            rec.sid = rec._create_sip_account(username=rec.username, password=rec.password)
                        # Don't keep SIP password in Odoo.
                        rec.with_context(skip_sync=True).password = '*' * len(rec.password)
                except Exception as e:
                    if 'A strong password is required' in str(e):
                        msg = 'A strong password is required. It must have a minimum length of 12, at least one number, uppercase char and lowercase character.'
                        raise ValidationError(msg)
                    else:
                        raise ValidationError(format_connect_response(e))
        for connect_user in recs:
            connect_user.manage_group()
        if recs and not self.env.context.get('no_clear_cache'):
            if release.version_info[0] >= 17:
                self.env.registry.clear_cache()
            else:
                self.clear_caches()
        return recs

    def delete_sip_account(self):
        self.ensure_one()
        if not self.sid:
            logger.warning(
                'Attempt to delete SIP account %s (%s) without SID!', self.id, self.name)
            return
        try:
            client = self.env['connect.settings'].get_client()
            credential = client.sip.credential_lists(
                self.domain.cred_list_sid).credentials(self.sid).delete()
            debug(self, 'Deleted SIP account {}.'.format(self.username))
            return True
        except Exception as e:
            if 'not found' in str(e):
                logger.warning('SIP account %s was not present in Twilio.', self.username)
            else:
                raise ValidationError(format_connect_response(e))

    def unlink(self):
        for rec in self:
            rec.delete_sip_account()
        self.manage_group('remove')
        res = super().unlink()
        if res and not self.env.context.get('no_clear_cache'):
            if release.version_info[0] >= 17:
                self.env.registry.clear_cache()
            else:
                self.clear_caches()
        return res

    def _update_sip_password(self, password):
        self.ensure_one()
        if not self.sid:
            logger.warning('SIP account %s SID not set, not updating.', self.id)
            return
        try:
            client = self.env['connect.settings'].get_client()
            credential = client.sip.credential_lists(
                self.domain.cred_list_sid).credentials(self.sid).update(password=password)
        except Exception as e:
            if 'A strong password is required.' in str(e):
                msg = 'A strong password is required. It must have a minimum length of 12, at least one number, uppercase char and lowercase character.'
                raise ValidationError(msg)
            elif 'not found' in str(e):
                # Twilio user is not present, create it.
                self._create_sip_account(self.username, password)
            else:
                raise ValidationError(format_connect_response(e))

    def manage_group(self, action='add'):
        if self.user and self.user.has_group('base.group_system') and self.user.has_group('base.group_erp_manager'):
            group_connect_admin = self.env.ref('connect.group_connect_admin')
            if action == 'add':
                group_connect_admin.write({'users': [(4, self.user.id)]})
            else:
                group_connect_admin.with_context(install_mode=True).write({'users': [(3, self.user.id)]})
        elif self.user:
            group_connect_user = self.env.ref('connect.group_connect_user')
            if action == 'add':
                group_connect_user.write({'users': [(4, self.user.id)]})
            else:
                group_connect_user.with_context(install_mode=True).write({'users': [(3, self.user.id)]})

    def write(self, vals):
        if 'user' in vals.keys():
            self.manage_group('remove')
        if self.env.context.get('skip_sync'):
            res = super().write(vals)
            self.manage_group()
            return res
        if 'username' in vals:
            raise ValidationError('Username cannot be changed!')
        for rec in self:
            sip_enabled = vals.get('sip_enabled', rec.sip_enabled)
            client_enabled = vals.get('client_enabled', rec.client_enabled)
            if not sip_enabled or not client_enabled:
                vals.update({
                    'ring_first': 'sip' if sip_enabled else 'client',
                    'ring_second': False,
                })
            if vals.get('sip_enabled') is False and rec.sid:
                rec.delete_sip_account()
                vals['sid'] = False
                vals['password'] = False
            if vals.get('password'):
                if rec.sid:
                    rec._update_sip_password(vals['password'])
                else:
                    # SIP was enabled, create SIP user account.
                    vals['sid'] = self._create_sip_account(rec.username, vals['password'])
                # Don't keep SIP password in Odoo.
                vals['password'] = '*' * len(vals['password'])
        res = super().write(vals)
        self.manage_group()
        if res and not self.env.context.get('no_clear_cache'):
            if release.version_info[0] >= 17:
                self.env.registry.clear_cache()
            else:
                self.clear_caches()
        return res

    def _get_name(self):
        for rec in self:
            rec.name = rec.user.name if rec.user else rec.username

    @api.constrains('username')
    def _check_username(self):
        for rec in self:
            if not rec.username.isalnum():
                raise ValidationError('Username must be alphanumeric!')

    def render(self, request={}, params={}):
        self.ensure_one()
        
        # Debug logging for transfer detection
        logger.info(f'=== USER RENDER CALLED FOR {self.name} ===')
        logger.info(f'Request params: Direction={request.get("Direction")}, CallSid={request.get("CallSid")}')
        logger.info(f'Params: Direction={params.get("Direction")}, ParentCallSid={params.get("ParentCallSid")}')
        
        channel = self.env['connect.channel'].search([('sid', '=', request.get('CallSid'))])
        call = channel.call
        
        # TRANSFER DETECTION: Check if this is a transfer redirect to our extension
        is_transfer_redirect = self._detect_transfer_redirect(request, params, call)
        if is_transfer_redirect:
            original_call = self._find_original_call_for_transfer(request, params)
            if original_call:
                logger.info(f'TRANSFER DETECTED: Extension {self.exten.number} receiving transfer from call {original_call.id}')
                # Store this user as transfer target for later completion tracking
                if self.user:
                    original_call.add_transferred_user(self.user)
                    # Store the redirect call SID for completion tracking
                    original_call.store_transfer_context(request.get('CallSid'), self.user)
                    logger.info(f'Added {self.user.login} as transfer target for call {original_call.id}')
            else:
                logger.warning(f'Could not find original call for transfer redirect to {self.name}')
        # Check callerid for client calls - but for transfer redirects, use the original external caller
        is_transfer_redirect = (
            params.get('Direction') == 'outbound-dial' and 
            params.get('ParentCallSid') and
            request.get('Direction') == 'outbound-dial'
        )
        
        if is_transfer_redirect:
            # For transfer redirects, the original external caller info is in params
            callerId = params.get('Called', request.get('Called', ''))  # The original external number
            caller_name = None  # No name available for external callers
            logger.info(f'Transfer redirect: Using external caller ID {callerId}')
        else:
            # Normal call logic
            user = self.env['connect.user'].get_user_by_uri(request.get('Caller'))
            caller_name = params.get('CallerName', False)
            if user:
                callerId = user.exten.number or ''
                if not callerId:
                    logger.warning('Exten not set for user %s', user.name)
                caller_name = user.name
            else:
                callerId = request.get('Caller')
        api_url = self.env['connect.settings'].sudo().get_param('api_url')
        record_status_url = urljoin(api_url, 'twilio/webhook/recordingstatus')
        status_url = urljoin(api_url, 'twilio/webhook/callstatus')
        # Add action URL for direct calls to prevent voicemail fall-through on completed calls
        action_url = urljoin(api_url, f'twilio/webhook/connect.user/call_action/{self.id}')
        response = VoiceResponse()
        
        # Greet the caller
        if self.greeting_message:
            self.get_greeting_message(response)
        
        # Create dial elements only for enabled device types
        dial_sip = None
        dial_client = None
        
        # Only create SIP dial if SIP is enabled
        if self.sip_enabled:
            # For transfer redirects, use dial_complete action URL for completion tracking
            # For regular calls, use call_action URL to prevent voicemail fall-through
            if is_transfer_redirect:
                dial_action_url = urljoin(api_url, 'connect/dial_complete')
            else:
                dial_action_url = action_url
            
            dial_sip_kwargs = {'timeout': self.sip_ring_timeout, 'callerId': callerId}
            if dial_action_url:
                dial_sip_kwargs['action'] = dial_action_url
                dial_sip_kwargs['method'] = 'POST'
            if self.record_calls:
                dial_sip_kwargs.update({
                    'recordingStatusCallback': record_status_url,
                    'record': 'record-from-answer-dual'
                })
            dial_sip = Dial(**dial_sip_kwargs)
            dial_sip.sip(
                'sip:{}'.format(self.uri),
                statusCallbackEvent='initiated completed',
                statusCallback=status_url)

        # Only create client dial if client is enabled
        if self.client_enabled:
            # For transfer redirects, use dial_complete action URL for completion tracking
            # For regular calls, use call_action URL to prevent voicemail fall-through
            if is_transfer_redirect:
                dial_action_url = urljoin(api_url, 'connect/dial_complete')
            else:
                dial_action_url = action_url
            
            dial_client_kwargs = {'timeout': self.client_ring_timeout, 'callerId': callerId}
            if dial_action_url:
                dial_client_kwargs['action'] = dial_action_url
                dial_client_kwargs['method'] = 'POST'
            if self.record_calls:
                dial_client_kwargs.update({
                    'record': 'record-from-answer',
                    'recordingStatusCallback': record_status_url
                })
            dial_client = Dial(**dial_client_kwargs)
            client = Client(
                statusCallbackEvent='initiated completed',
                statusCallback=status_url)
            client.identity(self.uri)
            if caller_name:
                client.parameter(name='CallerName', value=caller_name)
            if call and call.partner:
                partner_id = call.partner.id
            elif channel and channel.caller_user:
                partner_id = channel.caller_user.partner_id.id
            else:
                partner_id = False
            client.parameter(name='Partner', value=partner_id)
            dial_client.append(client)

        # Add ring attempts in order, but only if the device type is enabled
        if self.ring_first == 'sip' and self.sip_enabled and dial_sip:
            response.append(dial_sip)
        elif self.ring_first == 'client' and self.client_enabled and dial_client:
            response.append(dial_client)
        
        if self.ring_second == 'sip' and self.sip_enabled and dial_sip:
            response.append(dial_sip)
        elif self.ring_second == 'client' and self.client_enabled and dial_client:
            response.append(dial_client)
        
        # Voicemail - allow for transfer redirects (external caller should get voicemail if no answer)
        # The previous logic was too restrictive - we should allow voicemail for failed transfers
        if self.voicemail_enabled:
            # The call voicemail
            voicemail_record_status_url = urljoin(api_url, 'twilio/webhook/vm_recordingstatus')
            response.pause(length=1)
            self.get_voicemail_prompt(response)
            response.record(
                maxLength=120,
                finishOnKey='#',
                playBeep=True,
                recordingStatusCallback=voicemail_record_status_url)
            
            # Debug log for transfer calls
            is_transfer_redirect = (
                params.get('Direction') == 'outbound-dial' and 
                params.get('ParentCallSid') and
                request.get('Direction') == 'outbound-dial' and
                call and call.direction == 'outgoing' and
                call.transferred_users
            )
            if is_transfer_redirect:
                logger.info(f'Allowing voicemail for transfer redirect call (SID: {request.get("CallSid")}) - target did not answer')
        
        debug(self, pretty_xml(response.to_xml()))
        return response.to_xml()
    
    def _detect_transfer_redirect(self, request, params, call):
        """
        Detect if this extension render is for a transfer redirect.
        Transfer redirects are identified by:
        1. No existing channel for this CallSid (new call from redirect)
        2. Recent transfer activity in the system 
        3. Call pattern suggesting a transfer
        """
        call_sid = request.get('CallSid')
        if not call_sid:
            return False
            
        # If we already have a channel for this SID, it's not a redirect
        if call:
            logger.info(f'Extension render for existing channel/call - not a redirect')
            return False
            
        # Look for recent calls with transferred_users that don't have this SID
        # This suggests a new redirect call for a transfer
        recent_transfers = self.env['connect.call'].search([
            ('transferred_users', '!=', False),
            ('create_date', '>=', fields.Datetime.now() - timedelta(minutes=5))  # Within last 5 minutes
        ])
        
        if recent_transfers:
            logger.info(f'Found {len(recent_transfers)} recent transfers - this may be a transfer redirect')
            return True
            
        logger.info(f'No indicators of transfer redirect found')
        return False
    
    def _find_original_call_for_transfer(self, request, params):
        """
        Find the original call that initiated this transfer redirect.
        Uses multiple strategies to identify the correct call.
        """
        call_sid = request.get('CallSid')
        
        # Strategy 1: Look for calls with this user in transferred_users that don't have a channel with this SID
        if self.user:
            potential_calls = self.env['connect.call'].search([
                ('transferred_users', 'in', [self.user.id]),
                ('create_date', '>=', fields.Datetime.now() - timedelta(minutes=5))
            ])
            
            for call in potential_calls:
                # Check if this call already has a channel with our SID
                existing_channel = call.channels.filtered(lambda c: c.sid == call_sid)
                if not existing_channel:
                    logger.info(f'Found original call {call.id} for transfer redirect (user in transferred_users)')
                    return call
        
        # Strategy 2: Look for recent calls with transferred_users but no completed transfer channels
        recent_calls = self.env['connect.call'].search([
            ('transferred_users', '!=', False),
            ('create_date', '>=', fields.Datetime.now() - timedelta(minutes=5)),
            ('status', 'not in', ['completed', 'failed', 'busy', 'no-answer'])
        ])
        
        for call in recent_calls:
            # Check if any transfer recipients have completed channels
            transfer_completed = False
            for user in call.transferred_users:
                user_channels = call.channels.filtered(lambda c: c.called_user and c.called_user.id == user.id)
                if user_channels.filtered(lambda c: c.status == 'completed'):
                    transfer_completed = True
                    break
            
            if not transfer_completed:
                logger.info(f'Found original call {call.id} for transfer redirect (no completed transfers yet)')
                return call
        
        logger.warning(f'Could not find original call for transfer redirect SID {call_sid}')
        return None

    @api.model
    def get_client_token(self):
        has_user_group = self.env.user.has_group('connect.group_connect_user')
        has_admin_group = self.env.user.has_group('connect.group_connect_admin')
        if not (has_user_group or has_admin_group):
            return False
        user = self.search([('user', '=', self.env.user.id)])
        if not user:
            return False
        if not user.client_enabled:
            return False
        account_sid = self.env['connect.settings'].sudo().get_param('account_sid')
        api_key = self.env['connect.settings'].sudo().get_param('twilio_api_key')
        api_secret = self.env['connect.settings'].sudo().get_param('twilio_api_secret')
        identity = user.uri
        token = AccessToken(account_sid, api_key, api_secret, identity=identity, ttl=3600)
        voice_grant = VoiceGrant(
            outgoing_application_sid=user.application.sid or user.domain.application.sid,
            outgoing_application_params={},
            incoming_allow=True,
        )
        token.add_grant(voice_grant)
        return token.to_jwt()

    @api.model
    def get_user_by_exten_number(self, search_query):
        # Called from Client.
        has_group = self.env.user.has_group
        if not any([has_group('connect.group_connect_user'), has_group('connect.group_connect_admin')]):
            raise ValidationError('Only Connect users can search other Connect users!')
        domain = [['exten_number', '=', search_query]]
        search_fields = ['id', 'name', 'exten_number', 'user']
        user = self.sudo().search_read(domain, search_fields, limit=1, order='exten_number asc')
        return user[0] if user else False

    @api.model
    # @tools.ormcache('userinfo') - psycopg2.InterfaceError: Cursor already closed
    def get_user_by_uri(self, userinfo):
        if not userinfo:
            # Return empty set.
            return self.env['connect.user']
        re_call_uri = re.compile(r'^(?:sip|client):([^\s@]+@[^\s;]+)(?:;[^&\s]+(?:&[^&\s]+)*)?')
        found_uri = re_call_uri.search(userinfo)
        if found_uri:
            user = self.env['connect.user'].search([
                ('uri', '=', found_uri.group(1))])
            debug(self, 'Found user: {} by {}.'.format(user.username, userinfo))
            return user
        # Return empty set.
        return self.env['connect.user']

    def create_extension(self):
        self.ensure_one()
        return self.env['connect.exten'].create_extension(self, 'user')

    @api.model
    def on_call_action(self, record_id, request):
        """Handle Dial completion for direct calls - prevents voicemail fall-through on completed calls"""
        logger.info(f'=== USER CALL ACTION HANDLER ===')
        logger.info(f'User: {record_id}, DialCallStatus: {request.get("DialCallStatus")}')
        logger.info(f'Request: {json.dumps(request, indent=2)}')
        
        response = VoiceResponse()
        user = self.browse(record_id)
        
        if request.get('DialCallStatus') == 'completed':
            # Call was completed - hang up external caller to prevent voicemail fall-through
            logger.info(f'Direct call completed - hanging up external caller')
            response.hangup()
        else:
            # Call was not completed - provide voicemail if enabled
            logger.info(f'Direct call not completed (status: {request.get("DialCallStatus")}) - checking voicemail settings')
            
            if user.voicemail_enabled:
                # Voicemail is enabled - provide voicemail with appropriate prompt
                api_url = self.env['connect.settings'].sudo().get_param('api_url')
                record_status_url = urljoin(api_url, 'twilio/webhook/vm_recordingstatus')
                
                response.pause(length=1)
                
                if user.voicemail_prompt:
                    # User has personalized voicemail prompt
                    personalized_prompt = user.render_voicemail_prompt()
                    system_voice = self.env['connect.settings'].get_system_voice()
                    processed_text = self.env['connect.settings'].process_pronunciation(personalized_prompt)
                    response.say(processed_text, voice=system_voice)
                    logger.info(f'Using personalized voicemail prompt for {user.name}')
                else:
                    # User has voicemail enabled but no personalized prompt - use generic with name
                    generic_prompt = f'{user.name} is not available. Please leave a message.'
                    system_voice = self.env['connect.settings'].get_system_voice()
                    processed_text = self.env['connect.settings'].process_pronunciation(generic_prompt)
                    response.say(processed_text, voice=system_voice)
                    logger.info(f'Using generic voicemail prompt for {user.name}')
                
                response.record(
                    maxLength=120,
                    finishOnKey='#',
                    playBeep=True,
                    recordingStatusCallback=record_status_url)
                    
            else:
                # Voicemail is completely disabled - generic message and hangup
                system_voice = self.env['connect.settings'].get_system_voice()
                processed_text = self.env['connect.settings'].process_pronunciation('Sorry, I could not connect your call. Please try again later. Goodbye!')
                response.say(processed_text, voice=system_voice)
                response.pause(length=1) 
                response.hangup()
                logger.info(f'Voicemail disabled for {user.name} - using generic hangup message')
        
        debug(self, pretty_xml(str(response)))
        return response

    def get_greeting_message(self, response):
        # Override in Elevenlabs module.
        self.ensure_one()
        system_voice = self.env['connect.settings'].get_system_voice()
        processed_text = self.env['connect.settings'].process_pronunciation(self.greeting_message)
        response.say(processed_text, voice=system_voice)

    def get_voicemail_prompt(self, response):
        self.ensure_one()
        voicemail_prompt = self.render_voicemail_prompt()
        system_voice = self.env['connect.settings'].get_system_voice()
        processed_text = self.env['connect.settings'].process_pronunciation(voicemail_prompt)
        response.say(processed_text, voice=system_voice)

    def render_voicemail_prompt(self):
        self.ensure_one()
        # Render user greeting.
        environment = jinja2.Environment()
        template = environment.from_string(self.voicemail_prompt)
        return template.render({'user': self})

    @api.onchange('domain')
    def _restrict_sip_domain_change(self):
        if self.sip_enabled and self.sid:
            raise ValidationError('You cannot change SIP domain for existing SIP account! Disable SIP account first!')

    @api.onchange('sip_enabled')
    def _make_blank_password(self):
        if self.sip_enabled:
            self.password = ''

    @api.onchange('sip_enabled', 'client_enabled')
    def set_ring_priority(self):
        if self.client_enabled and not self.sip_enabled:
            self.ring_first = 'client'
            self.ring_second = False
        elif not self.client_enabled and self.sip_enabled:
            self.ring_first = 'sip'
            self.ring_second = False
        elif self.client_enabled and self.sip_enabled:
            self.ring_first = 'client'
            self.ring_second = 'sip'
        else:
            self.ring_first = 'client'

    @api.onchange('ring_first')
    def on_change_ring_priority(self):
        if not self.client_enabled or not self.sip_enabled:
            return
        if self.ring_first == 'client':
            self.ring_second = 'sip'
        else:
            self.ring_second = 'client'

