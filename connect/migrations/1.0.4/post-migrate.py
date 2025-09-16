# -*- coding: utf-8 -*-
import logging
from odoo import api, SUPERUSER_ID

logger = logging.getLogger(__name__)

def migrate(cr, version):
    """
    ONE-TIME Migration to 1.0.4:
    - Update existing users' voicemail prompts to new generic version
    - Enable voicemail and call recording for all users
    - Update ring timeout to 25 seconds
    """
    env = api.Environment(cr, SUPERUSER_ID, {})

    new_prompt = "{{user.name}} is unable to take your call right now. Please leave a message after the tone."

    # Find all connect users
    users = env['connect.user'].search([])

    updated_prompts = 0
    enabled_voicemail = 0
    enabled_recording = 0
    updated_timeouts = 0

    for user in users:
        # Update voicemail prompt if different
        if user.voicemail_prompt != new_prompt:
            user.voicemail_prompt = new_prompt
            updated_prompts += 1

        # Enable voicemail if not already enabled
        if not user.voicemail_enabled:
            user.voicemail_enabled = True
            enabled_voicemail += 1

        # Enable call recording if not already enabled
        if not user.record_calls:
            user.record_calls = True
            enabled_recording += 1

        # Update SIP ring timeout to 25 seconds if different
        if user.sip_ring_timeout != 25:
            user.sip_ring_timeout = 25
            updated_timeouts += 1

    logger.info(f"ONE-TIME Migration to 1.0.4 completed:")
    logger.info(f"  - Updated {updated_prompts} user voicemail prompts")
    logger.info(f"  - Enabled voicemail for {enabled_voicemail} users")
    logger.info(f"  - Enabled call recording for {enabled_recording} users")
    logger.info(f"  - Updated SIP ring timeout for {updated_timeouts} users")
    logger.info(f"  - Total users processed: {len(users)}")