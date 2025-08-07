from odoo import models, fields, api
from twilio.twiml.voice_response import VoiceResponse, Dial
import logging

logger = logging.getLogger(__name__)

class PhoneWizard(models.TransientModel):
    _name = 'connect.transfer_wizard'
    _description = 'Transfer Wizard'

    phone_number = fields.Char(string='Phone Number', required=True)

    def action_confirm(self):
        # Legacy method for manual wizard usage
        return {'type': 'ir.actions.act_window_close'}

    @api.model
    def execute_transfer(self, phone_number, transfer_type, call_id=None, session_id=None):
        """
        Execute a call transfer using Twilio TwiML
        
        :param phone_number: Target phone number or extension
        :param transfer_type: 'blind' for immediate transfer, 'attended' for consultation
        :param call_id: Odoo call record ID (optional)
        :param session_id: Twilio Call SID for the active call
        :return: dict with success status and message
        """
        try:
            if not session_id:
                return {
                    'success': False,
                    'error': 'No active call session found'
                }

            # Get Twilio client
            client = self.env['connect.settings'].get_client()
            if not client:
                return {
                    'success': False,
                    'error': 'Twilio client not configured'
                }

            # Determine if phone_number is an extension or external number
            target_number = self._resolve_phone_number(phone_number)
            
            if transfer_type == 'blind':
                success = self._execute_blind_transfer(client, session_id, target_number)
            elif transfer_type == 'attended':
                success = self._execute_attended_transfer(client, session_id, target_number)
            else:
                return {
                    'success': False,
                    'error': 'Invalid transfer type. Must be "blind" or "attended"'
                }

            if success:
                # Log the transfer attempt
                self._log_transfer_attempt(call_id, phone_number, transfer_type, session_id)
                
                return {
                    'success': True,
                    'message': f'{transfer_type.capitalize()} transfer initiated to {phone_number}'
                }
            else:
                return {
                    'success': False,
                    'error': 'Transfer failed - unable to update call'
                }

        except Exception as e:
            logger.exception(f'Transfer execution failed: {e}')
            return {
                'success': False,
                'error': f'Transfer failed: {str(e)}'
            }

    def _resolve_phone_number(self, phone_number):
        """
        Convert extension numbers to SIP URIs or return phone numbers as-is
        """
        # Check if it's a numeric extension (internal)
        if phone_number.isdigit() and len(phone_number) <= 4:
            # Look up the extension in connect.exten
            extension = self.env['connect.exten'].search([('number', '=', phone_number)], limit=1)
            if extension and extension.dst._name == 'connect.user':
                # Get the SIP domain for this user
                domain = self.env['connect.settings'].sudo().get_param('sip_domain')
                if domain:
                    return f'sip:{phone_number}@{domain}'
            # If no SIP domain or extension not found, treat as phone number
            return phone_number
        else:
            # External phone number - ensure it has proper formatting
            if not phone_number.startswith('+'):
                phone_number = f'+1{phone_number}'  # Assume US number if no country code
            return phone_number

    def _execute_blind_transfer(self, client, session_id, target_number):
        """
        Execute immediate blind transfer using TwiML
        """
        try:
            response = VoiceResponse()
            dial = Dial()
            
            if target_number.startswith('sip:'):
                # Internal SIP extension
                dial.sip(target_number)
            else:
                # External phone number
                dial.number(target_number)
            
            response.append(dial)
            
            # Update the active call with new TwiML
            client.calls(session_id).update(twiml=str(response))
            logger.info(f'Blind transfer executed to {target_number} for call {session_id}')
            return True
            
        except Exception as e:
            logger.error(f'Blind transfer failed: {e}')
            return False

    def _execute_attended_transfer(self, client, session_id, target_number):
        """
        Execute attended transfer using conference rooms
        """
        try:
            # Generate unique conference name
            import uuid
            conference_name = f'transfer-{uuid.uuid4().hex[:8]}'
            
            # Create TwiML to put current call in conference
            response = VoiceResponse()
            response.say('Please hold while we connect you.')
            dial = Dial()
            dial.conference(conference_name, start_conference_on_enter=True)
            response.append(dial)
            
            # Update current call to join conference
            client.calls(session_id).update(twiml=str(response))
            
            # Create new call to target and put them in same conference
            new_call_response = VoiceResponse()
            new_call_response.say('You have an incoming transfer.')
            new_dial = Dial()
            new_dial.conference(conference_name, start_conference_on_enter=True)
            new_call_response.append(new_dial)
            
            # Initiate call to target
            if target_number.startswith('sip:'):
                to_number = target_number
            else:
                to_number = target_number
                
            # Get caller ID for outgoing call
            caller_id = self.env['connect.settings'].sudo().get_param('default_caller_id')
            
            client.calls.create(
                to=to_number,
                from_=caller_id,
                twiml=str(new_call_response)
            )
            
            logger.info(f'Attended transfer initiated to {target_number} via conference {conference_name}')
            return True
            
        except Exception as e:
            logger.error(f'Attended transfer failed: {e}')
            return False

    def _log_transfer_attempt(self, call_id, phone_number, transfer_type, session_id):
        """
        Log the transfer attempt for audit purposes
        """
        try:
            if call_id:
                call = self.env['connect.call'].browse(call_id)
                if call.exists():
                    # Add note to call record
                    message = f'{transfer_type.capitalize()} transfer attempted to {phone_number}'
                    call.message_post(body=message)
            
            logger.info(f'Transfer logged: {transfer_type} to {phone_number} for session {session_id}')
        except Exception as e:
            logger.warning(f'Failed to log transfer attempt: {e}')