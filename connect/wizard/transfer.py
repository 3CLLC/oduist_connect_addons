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
                try:
                    domain = self.env['connect.settings'].sudo().get_param('sip_domain')
                    if domain:
                        return f'sip:{phone_number}@{domain}'
                    else:
                        logger.warning(f'No SIP domain configured, treating extension {phone_number} as phone number')
                except Exception as e:
                    logger.warning(f'Could not get SIP domain: {e}')
            # If no SIP domain or extension not found, treat as phone number
            return phone_number
        else:
            # External phone number - ensure it has proper formatting
            if not phone_number.startswith('+'):
                # Try to get default country code from settings, fallback to US
                try:
                    default_country = self.env['connect.settings'].sudo().get_param('default_country_code') or '1'
                    phone_number = f'+{default_country}{phone_number}'
                except:
                    phone_number = f'+1{phone_number}'  # Fallback to US
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
            
            # Get caller ID for outgoing call with fallbacks
            caller_id = self._get_caller_id_for_transfer(session_id)
            
            # Initiate call to target
            client.calls.create(
                to=target_number,
                from_=caller_id,
                twiml=str(new_call_response)
            )
            
            logger.info(f'Attended transfer initiated to {target_number} via conference {conference_name}')
            return True
            
        except Exception as e:
            logger.error(f'Attended transfer failed: {e}')
            return False

    def _get_caller_id_for_transfer(self, session_id):
        """
        Get appropriate caller ID for transfer call with multiple fallbacks
        """
        try:
            # Try to get caller ID from current call
            client = self.env['connect.settings'].get_client()
            call_info = client.calls(session_id).fetch()
            original_from = call_info.from_
            if original_from:
                return original_from
        except Exception as e:
            logger.warning(f'Could not get original caller ID: {e}')

        try:
            # Fallback 1: Default caller ID from settings
            default_caller_id = self.env['connect.settings'].sudo().get_param('default_caller_id')
            if default_caller_id:
                return default_caller_id
        except:
            pass

        try:
            # Fallback 2: First available Twilio number
            client = self.env['connect.settings'].get_client()
            numbers = client.incoming_phone_numbers.list(limit=1)
            if numbers:
                return numbers[0].phone_number
        except:
            pass

        # Final fallback - this should rarely be reached
        logger.error('No caller ID could be determined for transfer')
        return '+15551234567'  # Generic fallback

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

    @api.model
    def validate_transfer_configuration(self):
        """
        Validate that transfer functionality is properly configured
        Returns dict with validation results
        """
        validation = {
            'twilio_configured': False,
            'sip_domain_configured': False,
            'caller_id_configured': False,
            'ready_for_transfers': False,
            'issues': []
        }

        try:
            # Check Twilio client
            client = self.env['connect.settings'].get_client()
            if client:
                validation['twilio_configured'] = True
            else:
                validation['issues'].append('Twilio client not configured')
        except Exception as e:
            validation['issues'].append(f'Twilio configuration error: {str(e)}')

        try:
            # Check SIP domain
            domain = self.env['connect.settings'].sudo().get_param('sip_domain')
            if domain:
                validation['sip_domain_configured'] = True
            else:
                validation['issues'].append('SIP domain not configured - internal extensions may not work')
        except Exception as e:
            validation['issues'].append(f'SIP domain configuration error: {str(e)}')

        try:
            # Check caller ID
            caller_id = self.env['connect.settings'].sudo().get_param('default_caller_id')
            if caller_id:
                validation['caller_id_configured'] = True
            else:
                validation['issues'].append('Default caller ID not configured')
        except Exception as e:
            validation['issues'].append(f'Caller ID configuration error: {str(e)}')

        # Determine if ready for transfers
        validation['ready_for_transfers'] = (
            validation['twilio_configured'] and 
            len(validation['issues']) == 0
        )

        return validation