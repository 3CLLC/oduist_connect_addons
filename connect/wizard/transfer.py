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

    @api.model
    def debug_user_identity(self, extension_number):
        """
        Debug method to understand how client identities work in your system
        """
        try:
            # Look up the extension
            extension = self.env['connect.exten'].search([('number', '=', extension_number)], limit=1)
            if not extension or not extension.dst or extension.dst._name != 'connect.user':
                return {'error': f'Extension {extension_number} not found or not pointing to user'}
            
            user = extension.dst
            debug_info = {
                'extension_number': extension_number,
                'extension_name': extension.name,
                'user_name': user.name,
                'user_uri': user.uri,
                'user_username': getattr(user, 'username', 'N/A'),
                'user_client_enabled': user.client_enabled,
                'user_id': user.id,
            }
            
            # Check if there's an Odoo user linked
            if hasattr(user, 'user') and user.user:
                debug_info['odoo_user_name'] = user.user.name
                debug_info['odoo_user_login'] = user.user.login
            
            # Try different client identity formats
            uri_parts = user.uri.split('@') if user.uri else []
            debug_info['possible_identities'] = {
                'full_uri': user.uri,
                'username_part': uri_parts[0] if uri_parts else None,
                'username_field': getattr(user, 'username', None),
                'user_id_format': f'user{user.id}',
                'extension_format': f'ext{extension_number}',
            }
            
            logger.info(f'Client identity debug: {debug_info}')
            return debug_info
            
        except Exception as e:
            logger.error(f'Debug user identity failed: {e}', exc_info=True)
            return {'error': str(e)}

    @api.model
    def debug_current_call_state(self, session_id):
        """
        Debug the current state of a call before and after transfer
        """
        try:
            client = self.env['connect.settings'].get_client()
            call = client.calls(session_id).fetch()
            
            debug_info = {
                'call_sid': call.sid,
                'status': call.status,
                'direction': call.direction,
                'from_number': call.from_,
                'to_number': call.to,
                'start_time': str(call.start_time) if call.start_time else None,
                'end_time': str(call.end_time) if call.end_time else None,
                'duration': call.duration,
                'price': call.price,
                'answered_by': getattr(call, 'answered_by', 'N/A')
            }
            
            logger.info(f'Call state debug for {session_id}: {debug_info}')
            return debug_info
            
        except Exception as e:
            logger.error(f'Could not get call state: {e}')
            return {'error': str(e)}

    def _resolve_phone_number(self, phone_number):
        """
        Convert extension numbers to Twilio Client identities with enhanced debugging
        """
        logger.info(f'Resolving phone number: {phone_number}')
        
        # Check if it's a numeric extension (internal)
        if phone_number.isdigit() and len(phone_number) <= 4:
            # Get detailed debug info
            debug_info = self.debug_user_identity(phone_number)
            logger.info(f'Extension debug info: {debug_info}')
            
            # Look up the extension in connect.exten
            extension = self.env['connect.exten'].search([('number', '=', phone_number)], limit=1)
            logger.info(f'Found extension: {extension.name if extension else "None"}')
            
            if extension and extension.dst and extension.dst._name == 'connect.user':
                user = extension.dst
                logger.info(f'Extension points to user: {user.name} (URI: {user.uri})')
                
                # Try multiple identity formats based on your system
                possible_identities = []
                
                if hasattr(user, 'username') and user.username:
                    possible_identities.append(f'client:{user.username}')
                    logger.info(f'Added username identity: client:{user.username}')
                
                if hasattr(user, 'uri') and user.uri:
                    # Extract client identity from URI (remove @domain part)
                    client_identity = user.uri.split('@')[0] if '@' in user.uri else user.uri
                    possible_identities.append(f'client:{client_identity}')
                    logger.info(f'Added URI-based identity: client:{client_identity}')
                
                # Try user ID format
                possible_identities.append(f'client:user{user.id}')
                logger.info(f'Added user ID identity: client:user{user.id}')
                
                # For now, let's use the URI-based one (what we were using before)
                if user.uri:
                    client_identity = user.uri.split('@')[0] if '@' in user.uri else user.uri
                    client_target = f'client:{client_identity}'
                    logger.info(f'Extension {phone_number} resolved to Twilio Client: {client_target}')
                    logger.info(f'Other possible identities to try: {possible_identities}')
                    return client_target
                else:
                    # Fallback client identity based on extension
                    client_target = f'client:user{phone_number}'
                    logger.info(f'No URI found, using fallback client identity: {client_target}')
                    return client_target
            
            else:
                logger.warning(f'Extension {phone_number} not found or not pointing to user')
                # Still try as client identity - maybe it's a valid extension
                client_target = f'client:user{phone_number}'
                logger.info(f'Using fallback client identity: {client_target}')
                return client_target
        else:
            # External phone number - ensure it has proper formatting
            if not phone_number.startswith('+'):
                # Try to get default country code from settings, fallback to US
                try:
                    default_country = self.env['connect.settings'].sudo().get_param('default_country_code') or '1'
                    phone_number = f'+{default_country}{phone_number}'
                except:
                    phone_number = f'+1{phone_number}'  # Fallback to US
            
            logger.info(f'External number resolved to: {phone_number}')
            return phone_number

    def _execute_blind_transfer(self, client, session_id, target_number):
        """
        Execute immediate blind transfer using extension render method (like ElevenLabs)
        """
        try:
            logger.info(f'Executing blind transfer to {target_number} for session {session_id}')
            
            if target_number.startswith('client:'):
                # Extract extension number from client identity
                # We need to find which extension this client identity maps to
                extension_number = self._find_extension_by_client_identity(target_number)
                if extension_number:
                    return self._execute_extension_transfer(client, session_id, extension_number, 'blind')
                else:
                    logger.error(f'Could not find extension for client identity: {target_number}')
                    return False
            else:
                # External number - use our original TwiML approach
                response = VoiceResponse()
                dial = Dial(timeout=30)
                dial.number(target_number)
                response.append(dial)
                
                twiml_str = str(response)
                logger.info(f'Generated external transfer TwiML: {twiml_str}')
                
                result = client.calls(session_id).update(twiml=twiml_str)
                logger.info(f'External transfer executed: {result}')
                return True
            
        except Exception as e:
            logger.error(f'Blind transfer failed: {e}', exc_info=True)
            return False

    def _execute_attended_transfer(self, client, session_id, target_number):
        """
        Execute attended transfer using extension render method
        """
        try:
            logger.info(f'Executing attended transfer to {target_number} for session {session_id}')
            
            if target_number.startswith('client:'):
                # Extract extension number from client identity
                extension_number = self._find_extension_by_client_identity(target_number)
                if extension_number:
                    return self._execute_extension_transfer(client, session_id, extension_number, 'attended')
                else:
                    logger.error(f'Could not find extension for attended transfer: {target_number}')
                    return False
            else:
                # External number - use TwiML approach with announcement
                response = VoiceResponse()
                response.say('Connecting your call now.')
                dial = Dial(timeout=30)
                dial.number(target_number)
                response.append(dial)
                
                twiml_str = str(response)
                logger.info(f'Generated external attended transfer TwiML: {twiml_str}')
                
                result = client.calls(session_id).update(twiml=twiml_str)
                logger.info(f'External attended transfer executed: {result}')
                return True
                
        except Exception as e:
            logger.error(f'Attended transfer failed: {e}', exc_info=True)
            return False

    def _find_extension_by_client_identity(self, client_identity):
        """
        Find extension number from client identity (reverse lookup) - fixed search
        """
        try:
            # Remove 'client:' prefix
            identity = client_identity.replace('client:', '')
            
            # Look for user with matching username or URI part
            user = self.env['connect.user'].search([
                '|',
                ('username', '=', identity),
                ('uri', 'like', f'{identity}@')
            ], limit=1)
            
            if user:
                # Find extension pointing to this user using stored fields
                # We can't search on 'dst' directly, so search by model and res_id
                extension = self.env['connect.exten'].search([
                    ('model', '=', 'connect.user'),
                    ('res_id', '=', user.id)
                ], limit=1)
                
                if extension:
                    logger.info(f'Found extension {extension.number} for client identity {client_identity}')
                    return extension.number
            
            logger.warning(f'Could not find extension for client identity: {client_identity}')
            return None
            
        except Exception as e:
            logger.error(f'Error finding extension by client identity: {e}')
            return None

    def _execute_extension_transfer(self, client, session_id, extension_number, transfer_type):
        """
        Execute transfer with comprehensive debugging
        """
        try:
            logger.info(f'=== STARTING {transfer_type.upper()} TRANSFER ===')
            logger.info(f'Session ID: {session_id}')
            logger.info(f'Target Extension: {extension_number}')
            
            # Debug call state BEFORE transfer
            logger.info('=== CALL STATE BEFORE TRANSFER ===')
            pre_transfer_state = self.debug_current_call_state(session_id)
            
            # Find the extension
            extension = self.env['connect.exten'].search([('number', '=', extension_number)], limit=1)
            if not extension:
                logger.error(f'Extension {extension_number} not found')
                return False
            
            # Get the user this extension points to
            if not extension.dst or extension.dst._name != 'connect.user':
                logger.error(f'Extension {extension_number} does not point to a connect.user')
                return False
                
            user = extension.dst
            logger.info(f'Extension {extension_number} points to user: {user.name}')
            logger.info(f'User URI: {user.uri}')
            logger.info(f'User username: {user.username}')
            logger.info(f'User client_enabled: {user.client_enabled}')
            
            # Create transfer TwiML
            response = VoiceResponse()
            
            if transfer_type == 'attended':
                response.say('Please hold while we connect your call.')
                logger.info('Added hold message for attended transfer')
            
            dial = Dial(timeout=30)
            logger.info('Created Dial with 30 second timeout')
            
            from twilio.twiml.voice_response import Client
            client_elem = Client()
            client_elem.identity(user.uri)
            dial.append(client_elem)
            response.append(dial)
            
            # Fallback
            response.say('The person you are trying to reach is not available.')
            response.hangup()
            
            twiml_str = str(response)
            logger.info(f'=== GENERATED TWIML ===')
            logger.info(f'TwiML: {twiml_str}')
            logger.info(f'TwiML Length: {len(twiml_str)} characters')
            
            # Update the call
            logger.info('=== UPDATING CALL WITH TWIML ===')
            logger.info(f'About to update call {session_id}')
            
            result = client.calls(session_id).update(twiml=twiml_str)
            
            logger.info(f'=== CALL UPDATE RESULT ===')
            logger.info(f'Update result type: {type(result)}')
            logger.info(f'Update result: {result}')
            
            # Debug call state AFTER transfer
            import time
            time.sleep(2)  # Wait a moment for state to change
            logger.info('=== CALL STATE AFTER TRANSFER (2 sec delay) ===')
            post_transfer_state = self.debug_current_call_state(session_id)
            
            # Compare states
            logger.info('=== STATE COMPARISON ===')
            if pre_transfer_state.get('status') != post_transfer_state.get('status'):
                logger.info(f'Call status changed: {pre_transfer_state.get("status")} -> {post_transfer_state.get("status")}')
            else:
                logger.info(f'Call status unchanged: {post_transfer_state.get("status")}')
            
            logger.info(f'=== TRANSFER COMPLETE ===')
            return True
            
        except Exception as e:
            logger.error(f'=== TRANSFER FAILED WITH EXCEPTION ===')
            logger.error(f'Exception: {e}', exc_info=True)
            return False

    def _get_caller_id_for_transfer(self, session_id):
        """
        Get appropriate caller ID for transfer call with multiple fallbacks
        """
        try:
            # Try to get caller ID from current call
            client = self.env['connect.settings'].get_client()
            call_info = client.calls(session_id).fetch()
            original_to = call_info.to  # Use the 'to' number (your Twilio number)
            logger.info(f'Using original call TO number as caller ID: {original_to}')
            if original_to:
                return original_to
        except Exception as e:
            logger.warning(f'Could not get original call info: {e}')

        try:
            # Fallback 1: Default caller ID from settings
            default_caller_id = self.env['connect.settings'].sudo().get_param('default_caller_id')
            if default_caller_id:
                logger.info(f'Using default caller ID: {default_caller_id}')
                return default_caller_id
        except Exception as e:
            logger.warning(f'Could not get default caller ID: {e}')

        try:
            # Fallback 2: First available Twilio number
            client = self.env['connect.settings'].get_client()
            numbers = client.incoming_phone_numbers.list(limit=1)
            if numbers:
                logger.info(f'Using first Twilio number: {numbers[0].phone_number}')
                return numbers[0].phone_number
        except Exception as e:
            logger.warning(f'Could not get Twilio numbers: {e}')

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