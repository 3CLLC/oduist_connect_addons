# -*- coding: utf-8 -*-

import logging
from urllib.parse import urljoin
from odoo import fields, models, api, release
from twilio.twiml.voice_response import Gather, VoiceResponse, Say, Client, Sip, Dial
from .twiml import pretty_xml
from .settings import debug

logger = logging.getLogger(__name__)

class CallflowChoice(models.Model):
    _name = 'connect.callflow_choice'
    _description = 'Callflow Choice'

    callflow = fields.Many2one('connect.callflow', required=True, ondelete='cascade')
    choice_digits = fields.Char(required=True)
    exten = fields.Many2one('connect.exten', ondelete='restrict', required=True)
    speech = fields.Char()


class CallFlow(models.Model):
    _name = 'connect.callflow'
    _description = 'Call Flow'
    _order = 'name asc'

    name = fields.Char(required=True)
    exten = fields.Many2one('connect.exten', ondelete='set null', readonly=True)
    exten_number = fields.Char(related='exten.number', store=True)
    language = fields.Char(default='en-US', required=True)
    gather_input = fields.Boolean()
    gather_input_type = fields.Selection(string='Input Type',
        selection=[
            ('dtmf speech', 'DTMF + speech'),
            ('dtmf', 'DTMF'),
            ('speech', 'Speech')
        ], required=True, default='dtmf speech')
    gather_timeout = fields.Integer(string='Timeout', default=5)
    gather_hints = fields.Char('Hints', default='This is a phrase I expect to hear, department name or extension number')
    prompt_message = fields.Text('Prompt Message',
        default='Welcome to our company! Please enter the extension number of person '
                'you wish to dial or wait 5 seconds till I start connecting your call')
    invalid_input_message = fields.Text(default='We received wrong input. Please try again!')
    gather_digits = fields.Integer(required=True, default=1)
    choices = fields.One2many('connect.callflow_choice', 'callflow')
    pre_transfer_message = fields.Text(
        string='Pre-Transfer Message',
        help='Message to play before transferring call. Insert any disclaimer language here.'
    )
    gather_action_url = fields.Char(compute='_get_gather_action_url')
    ring_users = fields.Many2many('connect.user')
    ring_timeout = fields.Integer(
        string='Ring Timeout', 
        default=30, 
        required=True,
        help='How long to ring users (in seconds) before going to voicemail'
    )
    record_calls = fields.Boolean()
    voicemail_prompt = fields.Text()
    voicemail_enabled = fields.Boolean()
    # fallback_extension

    def create_extension(self):
        self.ensure_one()
        return self.env['connect.exten'].create_extension(self, 'callflow')

    def _get_gather_action_url(self):
        api_url = self.env['connect.settings'].get_param('api_url')
        for rec in self:
            rec.gather_action_url = urljoin(api_url, 'twilio/webhook/callflow/{}/gather'.format(rec.id))

    @api.model
    def gather_action(self, flow_id, request):
        logger.info(f"GATHER_ACTION: Called for callflow {flow_id} - Digits: '{request.get('Digits')}', SpeechResult: '{request.get('SpeechResult')}'")
        callflow = self.browse(flow_id)
        choice = callflow.choices.filtered(
            lambda x: x.choice_digits == request.get('Digits') or
                (x.speech and request.get('SpeechResult') and x.speech in
                request.get('SpeechResult', '')))
        if not choice:
            # Check if this is a timeout (no digits received) vs invalid input
            digits = request.get('Digits')
            if not digits:
                # This is a gather timeout - no user input received
                # Set ring_group pattern for timeout scenario if callflow has ring_users
                parent_call_sid = request.get('CallSid')
                if parent_call_sid and callflow.ring_users:
                    parent_call = self.env['connect.call'].search([
                        ('channels.sid', '=', parent_call_sid)
                    ], limit=1)
                    
                    if parent_call and not parent_call.call_pattern:
                        parent_call.call_pattern = 'ring_group'
                        
                        # Set webhook expectation for ring group channels (timeout scenario)
                        expected_count = len(callflow.ring_users)
                        parent_call._set_webhook_expectation('ring_group', {
                            'expected_count': expected_count,
                            'received_count': 0,
                            'callflow_id': callflow.id,
                            'source': 'gather_timeout'
                        })
                        
                        logger.info(f"Call {parent_call.id}: Pattern set to 'ring_group' via gather timeout (no user input) - expecting {expected_count} channels")
                
                # Render ring_users if available, otherwise fallback  
                return callflow.render(request=request, params={'gather_timeout': True})
            else:
                # This is invalid input (digits received but no matching choice)
                logger.warning('Gather choice digits: %s, speech: %s not found in Call Flow %s',
                    request.get('Digits'), request.get('SpeechResult'), callflow.name)
                return callflow.render(request=request, params={'invalid_input': True})
        
        # EXPLICIT PATTERN TAGGING: Set call pattern based on user choice
        parent_call_sid = request.get('CallSid')
        if parent_call_sid:
            parent_call = self.env['connect.call'].search([
                ('channels.sid', '=', parent_call_sid)
            ], limit=1)
            
            if parent_call and choice:
                target_extension = choice[0].exten
                
                # Determine pattern based on what the user chose
                if (target_extension.model == 'connect.callflow' and 
                    target_extension.dst and target_extension.dst.ring_users):
                    # This choice leads to a ring group
                    parent_call.call_pattern = 'ring_group'
                    
                    # Set webhook expectation for ring group channels
                    expected_count = len(target_extension.dst.ring_users)
                    parent_call._set_webhook_expectation('ring_group', {
                        'expected_count': expected_count,
                        'received_count': 0,
                        'callflow_id': target_extension.dst.id,
                        'source': 'gather_action'
                    })
                    
                    logger.info(f"Call {parent_call.id}: Pattern set to 'ring_group' via gather_action (choice: {choice[0].choice_digits}) - expecting {expected_count} channels")
                else:
                    # This choice leads to a direct extension
                    parent_call.call_pattern = 'direct_call'
                    
                    # Clear any existing ring_group webhook expectations since pattern changed
                    parent_call._clear_webhook_expectations('ring_group')
                    
                    logger.info(f"Call {parent_call.id}: Pattern set to 'direct_call' via gather_action (choice: {choice[0].choice_digits})")
        
        # Play pre-transfer message if configured before rendering chosen extension
        if self.pre_transfer_message:
            response = VoiceResponse()
            system_voice = self.env['connect.settings'].get_system_voice()
            processed_text = self.env['connect.settings'].process_pronunciation(self.pre_transfer_message)
            response.say(processed_text, voice=system_voice, language=self.language)
            
            # Then redirect to the chosen extension
            api_url = self.env['connect.settings'].get_param('api_url')
            redirect_url = urljoin(api_url, f'twilio/webhook/exten/{choice[0].exten.id}')
            response.redirect(redirect_url)
            return response
        else:
            return choice[0].exten.render(request=request)

    def render(self, request={}, params={}):
        self.ensure_one()
        api_url = self.env['connect.settings'].sudo().get_param('api_url')
        voicemail_record_status_url = urljoin(api_url, 'twilio/webhook/vm_recordingstatus')
        status_url = urljoin(api_url, 'twilio/webhook/callstatus')
        action_url = urljoin(api_url, 'twilio/webhook/connect.callflow/call_action/{}'.format(self.id))
        record_status_url = urljoin(api_url, 'twilio/webhook/recordingstatus')
        invalid_input = params.get('invalid_input')
        gather_timeout = params.get('gather_timeout')
        response = VoiceResponse()
        if invalid_input:
            self.get_gather_invalid_input_message(response)
        if self.prompt_message and self.gather_input and not gather_timeout:
            gather = Gather(
                action=self.gather_action_url,
                method='POST',
                timeout=self.gather_timeout,
                numDigits=str(self.gather_digits),
                input=self.gather_input_type,
                language=self.language,
                actionOnEmptyResult=True
            )
            self.get_prompt_message(gather)
            response.append(gather)
            logger.info(f"CALLFLOW RENDER: Created gather element - action={self.gather_action_url}, timeout={self.gather_timeout}")
        elif self.prompt_message and not gather_timeout:
            self.get_prompt_message(response)
            logger.info(f"CALLFLOW RENDER: Created prompt without gather (gather_input={self.gather_input})")
        # Add ringall users
        if self.ring_users:
            # NOTE: Do NOT set call pattern here during initial render
            # Pattern should only be set when there's actual user input or genuine timeout from gather action
            
            callerId = request.get('Caller')
            # Hack to enable testing callflow from SIP or Client.
            if callerId.startswith('sip:') or callerId.startswith('client:'):
                # Take the default number
                callerId = self.env['connect.outgoing_callerid'].sudo().search(
                    [('is_default', '=', True)], limit=1).number
                if not callerId:
                    response = VoiceResponse()
                    system_voice = self.env['connect.settings'].get_system_voice()
                    processed_text = self.env['connect.settings'].process_pronunciation('Your must configure a default number for caller ID!')
                    response.say(processed_text, voice=system_voice)
                    return response
            if self.record_calls:
                dial = Dial(callerId=callerId, action=action_url, timeout=self.ring_timeout,
                        record='record-from-answer-dual', recordingStatusCallback=record_status_url)
            else:
                dial = Dial(callerId=callerId, action=action_url, timeout=self.ring_timeout)
            for user in self.ring_users:
                # Only add enabled device types
                if user.ring_first == 'sip' and user.sip_enabled:
                    dial.sip('sip:{}'.format(user.uri),
                            statusCallbackEvent='completed',
                            statusCallback=status_url)
                elif user.ring_first == 'client' and user.client_enabled:
                    client = Client(
                        statusCallbackEvent='completed',
                        statusCallback=status_url)
                    client.identity(user.uri)
                    client.parameter(name='CallerName', value=callerId)
                    dial.append(client)
                # Ring 2nd - only if different from first and enabled
                if (user.ring_second == 'sip' and user.sip_enabled and 
                    user.ring_second != user.ring_first):
                    dial.sip('sip:{}'.format(user.uri),
                            statusCallbackEvent='completed',
                            statusCallback=status_url)
                elif (user.ring_second == 'client' and user.client_enabled and 
                    user.ring_second != user.ring_first):
                    client = Client(
                        statusCallbackEvent='completed',
                        statusCallback=status_url)
                    client.identity(user.uri)
                    client.parameter(name='CallerName', value=callerId)
                    dial.append(client)
            
            # Play pre-transfer message if configured
            if self.pre_transfer_message:
                system_voice = self.env['connect.settings'].get_system_voice()
                processed_text = self.env['connect.settings'].process_pronunciation(self.pre_transfer_message)
                response.say(processed_text, voice=system_voice, language=self.language)
            
            response.append(dial)
        else:
            # No ring users set, just send to voicemail if enabled.
            if self.voicemail_enabled and self.voicemail_prompt:
                response.pause(length=1)
                self.get_voicemail_prompt_message(response)
                response.record(
                    maxLength=120,
                    finishOnKey='#',
                    playBeep=True,
                    recordingStatusCallback=voicemail_record_status_url)
            else:
                # No voicemail, just say sorry and hangup.
                system_voice = self.env['connect.settings'].get_system_voice()
                processed_text = self.env['connect.settings'].process_pronunciation('This callflow has no actions! Goodbye!')
                response.say(processed_text, voice=system_voice)
                response.pause(length=1)
                response.hangup()
        debug(self, pretty_xml(str(response)))
        return response

    def get_prompt_message(self, response):
        debug(self, 'Saying prompt message for Call Flow {}'.format(self.name))
        system_voice = self.env['connect.settings'].get_system_voice()
        processed_text = self.env['connect.settings'].process_pronunciation(self.prompt_message)
        logger.info(f'CallFlow get_prompt_message: Using voice={system_voice}, language={self.language}')
        response.say(processed_text, language=self.language, voice=system_voice)

    def get_gather_invalid_input_message(self, response):
        system_voice = self.env['connect.settings'].get_system_voice()
        processed_text = self.env['connect.settings'].process_pronunciation(self.invalid_input_message)
        response.say(processed_text, language=self.language, voice=system_voice)

    def get_voicemail_prompt_message(self, response):
        system_voice = self.env['connect.settings'].get_system_voice()
        processed_text = self.env['connect.settings'].process_pronunciation(self.voicemail_prompt)
        response.say(processed_text, language=self.language, voice=system_voice)

    @api.model
    def on_call_action(self, flow_id, request):
        response = VoiceResponse()
        if request.get('DialCallStatus') != 'completed':
            callflow = self.browse(flow_id)
            # The call was not connected, point to the voicemail
            if callflow.voicemail_prompt:
                api_url = self.env['connect.settings'].sudo().get_param('api_url')
                record_status_url = urljoin(api_url, 'twilio/webhook/vm_recordingstatus')
                response.pause(length=1)
                system_voice = self.env['connect.settings'].get_system_voice()
                processed_text = self.env['connect.settings'].process_pronunciation(callflow.voicemail_prompt)
                response.say(processed_text, language=callflow.language, voice=system_voice)
                response.record(
                    maxLength=120,
                    finishOnKey='#',
                    playBeep=True,
                    recordingStatusCallback=record_status_url)
            else:
                # No voicemail, just say sorry and hangup.
                system_voice = self.env['connect.settings'].get_system_voice()
                processed_text = self.env['connect.settings'].process_pronunciation('Sorry, I could not connect your call. Goodbye!')
                response.say(processed_text, voice=system_voice)
                response.pause(length=1)
                response.hangup()
        else:
            # Call was connected, just hangup if the call was hangup by
            # the called party and the caller is still here.
            response.hangup()
        debug(self, pretty_xml(str(response)))
        return response
