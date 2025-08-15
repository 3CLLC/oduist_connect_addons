from odoo import models, fields, api
from twilio.twiml.voice_response import VoiceResponse, Dial
from urllib.parse import urljoin
import logging

logger = logging.getLogger(__name__)

class CallForwardHandler(models.TransientModel):
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
                success = self._execute_blind_transfer(client, session_id, target_number, call_id)
            elif transfer_type == 'attended':
                success = self._execute_attended_transfer(client, session_id, target_number, call_id)
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
        Debug the current state of a call before and after transfer - fixed attributes
        """
        try:
            client = self.env['connect.settings'].get_client()
            call = client.calls(session_id).fetch()
            
            debug_info = {
                'call_sid': call.sid,
                'status': call.status,
                'direction': call.direction,
                'from_number': getattr(call, 'from_', getattr(call, 'from_formatted', 'Unknown')),
                'to_number': getattr(call, 'to', getattr(call, 'to_formatted', 'Unknown')),
                'start_time': str(call.start_time) if call.start_time else None,
                'end_time': str(call.end_time) if call.end_time else None,
                'duration': call.duration,
                'price': getattr(call, 'price', 'Unknown'),
                'answered_by': getattr(call, 'answered_by', 'N/A'),
                'parent_call_sid': getattr(call, 'parent_call_sid', 'N/A'),
                'queue_time': getattr(call, 'queue_time', 'N/A')
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

    def _execute_blind_transfer(self, client, session_id, target_number, call_id=None):
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
                    return self._execute_extension_transfer(client, session_id, extension_number, 'blind', call_id)
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

    def _execute_attended_transfer(self, client, session_id, target_number, call_id=None):
        """
        Execute attended transfer using extension render method
        """
        try:
            logger.info(f'Executing attended transfer to {target_number} for session {session_id}')
            
            if target_number.startswith('client:'):
                # Extract extension number from client identity
                extension_number = self._find_extension_by_client_identity(target_number)
                if extension_number:
                    return self._execute_extension_transfer(client, session_id, extension_number, 'attended', call_id)
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

    def _execute_extension_transfer(self, client, session_id, extension_number, transfer_type, call_id=None):
        """
        Execute transfer with different behavior for blind vs attended transfers
        """
        try:
            logger.info(f'=== STARTING {transfer_type.upper()} TRANSFER ===')
            logger.info(f'Session ID: {session_id}')
            logger.info(f'Target Extension: {extension_number}')
            
            # Debug call state BEFORE transfer
            logger.info('=== CALL STATE BEFORE TRANSFER ===')
            pre_transfer_state = self.debug_current_call_state(session_id)
            
            # Check if this is a child call with a parent
            parent_call_sid = pre_transfer_state.get('parent_call_sid')
            if parent_call_sid and parent_call_sid != 'N/A':
                logger.info(f'=== DETECTED PARENT CALL: {parent_call_sid} ===')
                logger.info('Current call is a child call - will update parent call instead')
                target_call_sid = parent_call_sid
                
                # Debug the parent call state
                logger.info('=== PARENT CALL STATE ===')
                parent_state = self.debug_current_call_state(parent_call_sid)
            else:
                logger.info('=== NO PARENT CALL DETECTED ===')
                logger.info('Will update current call')
                target_call_sid = session_id
            
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
            
            # Track the transfer in the call record
            if user.user:
                try:
                    # Try to find the call record from the session_id
                    call = None
                    if call_id:
                        call = self.env['connect.call'].browse(call_id)
                        logger.info(f'Using provided call_id {call_id} for transfer tracking')
                    
                    if not call or not call.exists():
                        # Fallback: Find call by looking up channel with session_id
                        channel = self.env['connect.channel'].search([('sid', '=', session_id)], limit=1)
                        if channel and channel.call:
                            call = channel.call
                            logger.info(f'Found call {call.id} via channel lookup for session {session_id}')
                        else:
                            logger.warning(f'No call found for session {session_id}')
                    
                    if call and call.exists():
                        try:
                            call.add_transferred_user(user.user)
                            logger.info(f'Added transfer target {user.user.login} to call {call.id}')
                        except Exception as e:
                            logger.warning(f'Could not add transfer user (concurrent update): {e}')
                        
                        try:
                            # Store transfer context for webhook processing (use target_call_sid as key)
                            call.store_transfer_context(target_call_sid, user.user)
                            logger.info(f'Stored transfer context for call {target_call_sid} -> {user.user.login}')
                        except Exception as e:
                            logger.warning(f'Could not store transfer context: {e}')
                        
                        try:
                            # EXPLICIT PATTERN TAGGING: Ensure call pattern is set for transfers
                            if not call.call_pattern:
                                call.call_pattern = 'direct_call'  # Transfers only happen from direct calls
                                logger.info(f'Call {call.id}: Set pattern to direct_call during transfer')
                        except Exception as e:
                            logger.warning(f'Could not set call pattern: {e}')
                    else:
                        logger.warning(f'Could not find call record to track transfer to {user.user.login}')
                except Exception as e:
                    logger.error(f'Failed to track transfer in call record: {e}', exc_info=True)
            
            # Determine if this is an outgoing call to use appropriate transfer method
            is_outgoing_call = False
            if call and call.exists():
                is_outgoing_call = call.direction == 'outgoing'
                logger.info(f'=== CALL ANALYSIS FOR TRANSFER ===')
                logger.info(f'Call ID: {call.id}, Direction: {call.direction} (outgoing={is_outgoing_call})')
                logger.info(f'Called: {call.called}, Caller: {call.caller}')
                logger.info(f'Call Pattern: {call.call_pattern}')
                logger.info(f'Number of channels: {len(call.channels)}')
                
                # Log current user context for debugging user-specific issues
                current_user = self.env.user
                logger.info(f'Current Odoo user: {current_user.login} (ID: {current_user.id})')
                
                # See if we can identify which connect.user is involved
                connect_user = self.env['connect.user'].search([('user', '=', current_user.id)], limit=1)
                if connect_user:
                    logger.info(f'Connect user: {connect_user.name} (URI: {connect_user.uri})')
                else:
                    logger.info('No connect.user found for current Odoo user')
            
            # Choose transfer method based on call type
            if is_outgoing_call and transfer_type == 'blind':
                logger.info('=== USING CONFERENCE BRIDGE FOR OUTGOING CALL ===')
                # Use conference approach targeting the external call leg
                result = self._execute_outgoing_conference_transfer(client, target_call_sid, user, call)
                logger.info(f'Conference transfer result: {result}')
                return result
            else:
                logger.info('=== USING TWIML TRANSFER METHOD ===')
                
                # Create different TwiML based on transfer type
                if transfer_type == 'blind':
                    twiml_str = self._create_blind_transfer_twiml(user, call if is_outgoing_call else None)
                    logger.info('Created BLIND transfer TwiML (immediate transfer)')
                else:
                    twiml_str = self._create_attended_transfer_twiml(user, target_call_sid)
                    logger.info('Created ATTENDED transfer TwiML (conference-based)')
                
                logger.info(f'=== GENERATED TWIML ===')
                logger.info(f'TwiML: {twiml_str}')
                logger.info(f'TwiML Length: {len(twiml_str)} characters')
                
                # Update the CORRECT call (parent if exists, otherwise current)
                logger.info('=== UPDATING CALL WITH TWIML ===')
                logger.info(f'About to update call {target_call_sid} ({"parent" if parent_call_sid else "current"})')
                
                result = client.calls(target_call_sid).update(twiml=twiml_str)
                
                logger.info(f'=== CALL UPDATE RESULT ===')
                logger.info(f'Update result: {result}')
                
                # For attended transfer, we need to handle the consultation phase
                if transfer_type == 'attended':
                    # The original recipient (you) should stay connected until you hang up
                    # The child call should continue until you decide to complete the transfer
                    logger.info('=== ATTENDED TRANSFER: Keeping original recipient connected ===')
                    
                logger.info(f'=== TRANSFER COMPLETE ===')
                return True
            
        except Exception as e:
            logger.error(f'=== TRANSFER FAILED WITH EXCEPTION ===')
            logger.error(f'Exception: {e}', exc_info=True)
            return False

    def _create_blind_transfer_twiml(self, user, outgoing_call=None):
        """
        For outgoing calls, use bridge transfer approach instead of TwiML modification
        This prevents external party disconnection by not modifying the original call flow
        """
        logger.info(f'=== CREATING BLIND TRANSFER TWIML ===')
        logger.info(f'Target user: {user.name} (URI: {user.uri})')
        logger.info(f'Outgoing call provided: {"Yes" if outgoing_call else "No"}')
        
        if outgoing_call and outgoing_call.direction == 'outgoing':
            logger.info(f'=== OUTGOING CALL DETECTED - USING BRIDGE TRANSFER ===')
            # For outgoing calls, return minimal TwiML and handle via bridge method
            response = VoiceResponse()
            response.say('Transfer initiated. Please stand by.')
            
            twiml_output = str(response)
            logger.info(f'Generated minimal TwiML for bridge transfer: {twiml_output}')
            return twiml_output
        
        # For incoming calls, use the standard TwiML approach
        logger.info(f'=== INCOMING CALL - USING STANDARD TWIML TRANSFER ===')
        response = VoiceResponse()
        response.say('Transferring your call now.')
        
        # Get the base URL for webhook callbacks
        api_url = self.env['connect.settings'].sudo().get_param('api_url')
        logger.info(f'API URL base: {api_url}')
        
        # Create action URL with transfer context for continuation logic
        action_url = urljoin(api_url, f'twilio/webhook/transfer_continuation')
        logger.info(f'Transfer continuation URL: {action_url}')
        
        # Create the transfer dial with action continuation
        dial = Dial(
            timeout=30,
            action=action_url,
            method='POST'
        )
        logger.info(f'Created Dial with 30s timeout and action URL')
        
        from twilio.twiml.voice_response import Client
        client_elem = Client()
        client_elem.identity(user.uri)
        dial.append(client_elem)
        logger.info(f'Added client identity to dial: {user.uri}')
        
        response.append(dial)
        logger.info(f'Appended dial element to response')
        
        # Critical: Add continuation TwiML that executes AFTER the dial completes
        response.say('Call could not be completed. Please try again.')
        response.hangup()
        logger.info(f'Added fallback TwiML for cases where transfer fails')
        
        twiml_output = str(response)
        logger.info(f'=== GENERATED TWIML (LENGTH: {len(twiml_output)}) ===')
        logger.info(f'TwiML Content: {twiml_output}')
        logger.info(f'=== END TWIML GENERATION ===')
        
        return twiml_output

    def _execute_outgoing_conference_transfer(self, client, call_sid, user, call):
        """
        Execute conference transfer for outgoing calls by moving the external call leg to conference
        This preserves the external connection by working with the existing call structure
        """
        try:
            logger.info(f'=== EXECUTING OUTGOING CONFERENCE TRANSFER ===')
            logger.info(f'Call SID: {call_sid}')
            logger.info(f'Transfer to user: {user.name} (URI: {user.uri})')
            
            # Step 1: Get the external call leg from transfer context (fast lookup)
            logger.info(f'=== GETTING EXTERNAL CALL LEG FROM CONTEXT ===')
            logger.info(f'Call ID: {call.id}, Direction: {call.direction}')
            
            external_call_sid = call.get_external_call_leg()
            
            if external_call_sid:
                logger.info(f'✓ Retrieved external call leg from context: {external_call_sid}')
            else:
                logger.error('❌ No external call leg stored in transfer context')
                logger.error('This indicates the outbound-dial channel was not properly stored during call setup')
                return False
            
            # Step 2: Create a conference to bridge the calls
            import uuid
            conference_name = f'outgoing-xfer-{call_sid[-8:]}-{int(__import__("time").time())}'
            logger.info(f'Conference name: {conference_name}')
            
            # Step 3: Move the EXTERNAL call leg to conference (preserves external party connection)
            logger.info(f'=== MOVING EXTERNAL CALL TO CONFERENCE ===')
            logger.info(f'Updating external call {external_call_sid} with conference TwiML')
            
            external_conference_response = VoiceResponse()
            external_conference_response.say('Please hold while we transfer your call.')
            
            dial = Dial()
            dial.conference(
                conference_name,
                startConferenceOnEnter=True,
                endConferenceOnExit=False,  # Don't end when external party leaves
                muted=False
            )
            external_conference_response.append(dial)
            
            # Update the EXTERNAL call to join the conference
            external_result = client.calls(external_call_sid).update(twiml=str(external_conference_response))
            logger.info(f'External call moved to conference: {external_result.status}')
            
            # Step 4: Create a new call to the transfer target to join the same conference
            logger.info(f'=== CREATING TRANSFER TARGET CALL ===')
            
            # Get caller ID for the transfer target call
            external_number = self._get_original_caller_for_transfer(call)
            if not external_number:
                external_number = call.called or '+15551234567'  # Fallback
            logger.info(f'Using external caller ID: {external_number}')
            
            # Create TwiML for the target user to join conference immediately
            target_response = VoiceResponse()
            target_response.say('You have an incoming transferred call.')
            
            target_dial = Dial()
            target_dial.conference(
                conference_name,
                startConferenceOnEnter=True,
                endConferenceOnExit=True  # End conference when target leaves
            )
            target_response.append(target_dial)
            
            # Get webhook URL for status callbacks
            api_url = self.env['connect.settings'].sudo().get_param('api_url')
            status_callback_url = urljoin(api_url, 'twilio/webhook/callstatus')
            
            # Create the call to the target with external caller ID
            target_call = client.calls.create(
                to=f'client:{user.uri}',
                from_=external_number,  # This shows external caller ID to Patrick
                twiml=str(target_response),
                status_callback=status_callback_url,
                status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
                status_callback_method='POST'
            )
            
            logger.info(f'Transfer target call created: {target_call.sid}')
            logger.info(f'Target will see caller ID: {external_number}')
            
            # Create channel record for the transfer target call
            self._create_transfer_target_channel(call, target_call.sid, user)
            
            # Step 5: Update the original Jason call to provide feedback and then hang up
            logger.info(f'=== RELEASING ORIGINAL CALLER ===')
            
            jason_response = VoiceResponse()
            jason_response.say('Transfer completed. You are now being disconnected.')
            jason_response.hangup()
            
            # Update Jason's call 
            jason_result = client.calls(call_sid).update(twiml=str(jason_response))
            logger.info(f'Original caller updated: {jason_result.status}')
            
            logger.info(f'=== CONFERENCE TRANSFER COMPLETE ===')
            logger.info(f'Conference: {conference_name}')
            logger.info(f'External call in conference: {external_call_sid}')
            logger.info(f'Target call created: {target_call.sid}')
            logger.info(f'Original caller released: {call_sid}')
            
            return True
            
        except Exception as e:
            logger.error(f'Conference transfer failed: {e}', exc_info=True)
            return False

    def _execute_outgoing_blind_transfer(self, client, call_sid, user, call):
        """
        Execute blind transfer for outgoing calls using Twilio's conference bridge approach
        This preserves the external connection by creating a 3-way bridge instead of replacing TwiML
        """
        try:
            logger.info(f'=== EXECUTING OUTGOING CALL BLIND TRANSFER ===')
            logger.info(f'Call SID: {call_sid}')
            logger.info(f'Transfer to user: {user.name} (URI: {user.uri})')
            
            # Get original external caller information for proper caller ID
            original_caller = self._get_original_caller_for_transfer(call)
            
            # For outgoing calls, we need to find and move the EXTERNAL call leg to conference
            # The current call_sid is the parent (internal), we need the child (external)
            
            # Step 1: Find the external call leg (outbound-dial direction) with retry logic
            logger.info(f'=== FINDING EXTERNAL CALL LEG FOR TRANSFER ===')
            logger.info(f'Call ID: {call.id}, Direction: {call.direction}')
            
            external_call_sid = None
            max_retries = 3
            retry_delay = 0.5  # seconds
            
            for attempt in range(max_retries):
                # Refresh call record to get latest channels by re-browsing
                call = self.env['connect.call'].browse(call.id)
                logger.info(f'Attempt {attempt + 1}: Call has {len(call.channels)} channels')
                
                # Also search for channels by ParentCallSid (more reliable than call.channels)
                parent_channel = call.channels.filtered(lambda c: c.technical_direction == 'inbound')
                if parent_channel:
                    child_channels = self.env['connect.channel'].search([('parent_sid', '=', parent_channel[0].sid)])
                    logger.info(f'Found {len(child_channels)} child channels via ParentCallSid search')
                    for channel in child_channels:
                        logger.info(f'  Child Channel: SID={channel.sid}, direction={channel.technical_direction}, status={channel.status}')
                        if channel.technical_direction == 'outbound-dial':
                            external_call_sid = channel.sid
                            logger.info(f'✓ Found external call leg via ParentCallSid: {external_call_sid}')
                            break
                
                # Fallback: search call.channels as before
                if not external_call_sid:
                    for i, channel in enumerate(call.channels):
                        logger.info(f'  Channel {i+1}: SID={channel.sid}, direction={channel.technical_direction}, status={channel.status}')
                        
                        if channel.technical_direction == 'outbound-dial':
                            external_call_sid = channel.sid
                            logger.info(f'✓ Found external call leg: {external_call_sid}')
                            break
                
                if external_call_sid:
                    break
                    
                if attempt < max_retries - 1:
                    logger.info(f'External call leg not found, waiting {retry_delay}s before retry...')
                    import time
                    time.sleep(retry_delay)
            
            if not external_call_sid:
                logger.error('❌ Could not find external call leg after retries')
                logger.error('Available channels: ' + ', '.join([f'{ch.sid}:{ch.technical_direction}' for ch in call.channels]))
                return False
            
            # Step 2: Create a conference to bridge the calls
            import uuid
            conference_name = f'outgoing-transfer-{call_sid[-8:]}'
            
            # Step 3: Move the EXTERNAL call leg to conference (this keeps external party connected)
            logger.info(f'Moving external call {external_call_sid} to conference {conference_name}')
            
            bridge_response = VoiceResponse()
            bridge_response.say('Please hold, transferring your call.')
            
            dial = Dial()
            dial.conference(
                conference_name,
                startConferenceOnEnter=True,
                endConferenceOnExit=False,  # Don't end when external caller leaves
                muted=False
            )
            bridge_response.append(dial)
            
            # Update the EXTERNAL call to join the conference
            result = client.calls(external_call_sid).update(twiml=str(bridge_response))
            logger.info(f'Moved external call {external_call_sid} to conference: {result}')
            
            # Step 4: Create a new call to the target extension to join the same conference
            logger.info(f'Creating new call to target extension {user.uri}')
            
            # Get caller ID for the new call - use the external number
            caller_id = original_caller or self._get_caller_id_for_transfer('')
            
            # Create TwiML for the target user to join conference immediately
            target_response = VoiceResponse()
            target_response.say('You have an incoming transfer.')
            
            target_dial = Dial()
            target_dial.conference(
                conference_name,
                startConferenceOnEnter=True,
                endConferenceOnExit=True  # End conference when target leaves
            )
            target_response.append(target_dial)
            
            # Get webhook URL for status callbacks
            api_url = self.env['connect.settings'].sudo().get_param('api_url')
            status_callback_url = urljoin(api_url, 'twilio/webhook/callstatus')
            
            # Create the call to the target
            target_call = client.calls.create(
                to=f'client:{user.uri}',
                from_=caller_id,
                twiml=str(target_response),
                status_callback=status_callback_url,
                status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
                status_callback_method='POST'
            )
            
            # Create a channel record for the transfer target call so completion can be tracked
            self._create_transfer_target_channel(call, target_call.sid, user)
            
            logger.info(f'OUTGOING BLIND TRANSFER: Successfully created bridge')
            logger.info(f'Conference: {conference_name}')
            logger.info(f'External call moved to conference: {external_call_sid}')
            logger.info(f'Target call created: {target_call.sid}')
            logger.info(f'Original caller ID preserved: {original_caller}')
            
            return result
            
        except Exception as e:
            logger.error(f'Outgoing blind transfer failed: {e}', exc_info=True)
            return False

    def _get_original_caller_for_transfer(self, call):
        """
        Extract the original external caller information for outgoing call transfers
        This ensures the transfer recipient sees the external caller, not the internal user
        """
        try:
            if not call or call.direction != 'outgoing':
                logger.info('Not an outgoing call, no original caller to extract')
                return None
            
            # For outgoing calls, the external party info should be in the called field or channels
            # Look for the external number from the child channel (outbound-dial direction)
            outbound_channel = None
            for channel in call.channels:  # Fixed: use 'channels' not 'channel_ids'
                if channel.technical_direction == 'outbound-dial':
                    outbound_channel = channel
                    break
            
            if outbound_channel:
                # The external number is the 'called' number in the outbound channel
                external_number = outbound_channel.called_number
                logger.info(f'Found external caller from outbound channel: {external_number}')
                return external_number
            
            # Fallback: try to get from call.called field
            if hasattr(call, 'called') and call.called:
                logger.info(f'Using call.called as external caller: {call.called}')
                return call.called
            
            logger.warning('Could not determine original external caller for outgoing call transfer')
            return None
            
        except Exception as e:
            logger.error(f'Error getting original caller for transfer: {e}')
            return None

    def _create_transfer_target_channel(self, call, target_call_sid, user):
        """
        Create a channel record for the transfer target call to enable completion tracking
        This ensures the system knows who answered/completed the transferred call
        """
        try:
            logger.info(f'Creating transfer target channel for call {target_call_sid}')
            
            # Find a parent channel to associate with
            parent_channel = None
            for channel in call.channels:
                if channel.technical_direction in ['inbound', 'outbound-dial']:
                    parent_channel = channel
                    break
            
            if not parent_channel:
                logger.warning(f'No suitable parent channel found for transfer target channel')
                return None
            
            # Create channel record for transfer target
            channel_data = {
                'sid': target_call_sid,
                'call': call.id,
                'parent_channel': parent_channel.id,
                'technical_direction': 'outbound-dial',
                'status': 'initiated',  # Will be updated by webhooks
                'duration': 0,
                'called_pbx_user': user.id,
                'called_user': user.user.id if user.user else None,
                'call_source': 'transfer',
                'caller': parent_channel.caller,  # Keep original caller info
                'called': user.uri,
            }
            
            # Use sudo to avoid permission issues during channel creation
            transfer_channel = self.env['connect.channel'].sudo().create(channel_data)
            
            logger.info(f'Created transfer target channel {transfer_channel.id} for user {user.name}')
            logger.info(f'Channel SID: {target_call_sid}, Call: {call.id}, User: {user.user.login if user.user else "No Odoo User"}')
            
            return transfer_channel
            
        except Exception as e:
            logger.error(f'Failed to create transfer target channel: {e}', exc_info=True)
            return None

    def _create_attended_transfer_twiml(self, user, call_sid):
        """
        Create TwiML for attended (consultation) transfer using conference
        """
        import uuid
        conference_name = f'transfer-{call_sid[-8:]}'  # Use last 8 chars of call ID
        
        response = VoiceResponse()
        response.say('Please hold while we connect you.')
        
        dial = Dial()
        dial.conference(
            conference_name,
            startConferenceOnEnter=True,
            endConferenceOnExit=True
        )
        response.append(dial)
        
        # Create a separate call to bring the target user into the conference
        self._initiate_conference_call(user, conference_name)
        
        return str(response)

    def _initiate_conference_call(self, user, conference_name):
        """
        Create a separate call to bring the transfer target into the conference
        """
        try:
            client = self.env['connect.settings'].get_client()
            
            # Get caller ID for the conference call
            caller_id = self._get_caller_id_for_transfer('')
            
            # Create TwiML for the target user to join conference
            target_response = VoiceResponse()
            target_response.say('You have an incoming transfer.')
            
            target_dial = Dial()
            target_dial.conference(
                conference_name,
                startConferenceOnEnter=True,
                endConferenceOnExit=False  # Don't end conference when target leaves
            )
            target_response.append(target_dial)
            
            # Get webhook URL for status callbacks
            api_url = self.env['connect.settings'].sudo().get_param('api_url')
            status_callback_url = urljoin(api_url, 'twilio/webhook/callstatus')
            
            # Create the call to the target
            call = client.calls.create(
                to=f'client:{user.uri}',
                from_=caller_id,
                twiml=str(target_response),
                status_callback=status_callback_url,
                status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
                status_callback_method='POST'
            )
            
            logger.info(f'Created conference call to target: {call.sid}')
            return call.sid
            
        except Exception as e:
            logger.error(f'Failed to create conference call: {e}')
            return None

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

    @api.model
    def handle_transfer_continuation(self, webhook_params):
        """
        Handle the action callback from transfer dial completion
        This is where we implement the continuation logic to preserve external connections
        """
        logger.info(f'=== HANDLING TRANSFER CONTINUATION ===')
        logger.info(f'Webhook parameters: {webhook_params}')
        
        call_sid = webhook_params.get('CallSid')
        dial_call_status = webhook_params.get('DialCallStatus')
        dial_call_sid = webhook_params.get('DialCallSid')
        
        logger.info(f'Call SID: {call_sid}')
        logger.info(f'Dial Status: {dial_call_status}')  
        logger.info(f'Dial Call SID: {dial_call_sid}')
        
        from twilio.twiml.voice_response import VoiceResponse
        response = VoiceResponse()
        
        if dial_call_status == 'completed':
            logger.info('Transfer completed successfully - call should continue normally')
            # Transfer was successful, the call should naturally continue
            # We don't need to add anything - Twilio will bridge the calls
            response.hangup()  # End the original call leg
            
        elif dial_call_status in ['busy', 'no-answer', 'failed', 'canceled']:
            logger.info(f'Transfer failed with status: {dial_call_status}')
            # Transfer failed - we could implement fallback logic here
            # For now, let the original call continue with a message
            response.say(f'Transfer could not be completed. The extension is {dial_call_status}.')
            response.say('You are being returned to the original caller.')
            # Don't hangup - let the original connection continue
            
        else:
            logger.warning(f'Unexpected dial call status: {dial_call_status}')
            response.say('There was an issue with the transfer. Please try again.')
            response.hangup()
        
        twiml_response = str(response)
        logger.info(f'Generated continuation TwiML: {twiml_response}')
        logger.info(f'=== END TRANSFER CONTINUATION HANDLING ===')
        
        return response