/** @odoo-module **/

import {useService} from "@web/core/utils/hooks"
import {Component, useState, onWillStart} from "@odoo/owl"
import {user} from "@web/core/user"

const uid = user.userId

class CallDetail extends Component {
    static template = 'connect.call_detail'
    static props = {
        call: Object
    }

    constructor() {
        super(...arguments)
        this.user = uid
        this.state = useState({
            call: this.props.call,
        })
    }

    setup() {
        super.setup()
        this.orm = useService('orm')
        this.action = useService('action')

        onWillStart(async () => {
            this.getCall(this.state.call.id)
        })
    }

    async getCall(id) {
        const fields = [
            "id",
            "duration_human",
            "called",
            "caller",
            "caller_user",
            "called_users",
            "partner",
            "direction",
            "create_date"
        ]
        const [call] = await this.orm.searchRead("connect.call", [["id", "=", id]], fields)
        this.state.call = call
    }

    async _createOpenPartner() {
        await this.getCall(this.state.call.id)
        if (this.state.call.partner) {
            this.action.doAction({
                res_id: this.state.call.partner[0],
                res_model: "res.partner",
                target: 'new',
                type: 'ir.actions.act_window',
                views: [[false, 'form']],
            })
        } else {
            const phone = this.state.call.called_users[0] === this.user ?
                this.state.call.caller : this.state.call.called
            let context = {
                connect_call_id: this.state.call.id,
                default_phone: phone,
                default_name: `Partner ${phone}`
            }
            this.action.doAction({
                context,
                res_model: 'res.partner',
                target: 'new',
                type: 'ir.actions.act_window',
                views: [[false, 'form']],
            })
        }
    }

    _OpenInCallHistory() {
        this.action.doAction({
            res_id: this.state.call.id,
            res_model: 'connect.call',
            target: 'new',
            type: 'ir.actions.act_window',
            views: [[false, 'form']],
        })
    }
}

export class Calls extends Component {
    static template = 'connect.calls'
    static props = {
        bus: Object,
    }
    static components = {CallDetail}

    constructor() {
        super(...arguments)
        this.bus = this.props.bus
    }

    setup() {
        super.setup()
        this.orm = useService('orm')
        this.action = useService('action')
        this.notification = useService('notification')
        this.user = uid
        this.favorites = []
        this.state = useState({
            calls: [],
            call: null,
        })

        onWillStart(async () => {
            this.bus.addEventListener('busCallsGetCalls', (ev) => this._getCalls(ev))
            this.bus.addEventListener('busCallsGetFavorites', (ev) => this._getFavorites(ev))
            this._getFavorites()
        })
    }

    async _getCalls() {
        this.state.calls = []
        const domain = ["|", "|", "|", ["caller_user", "=", this.user], ["called_users", "in", this.user], ["answered_user", "=", this.user], ["transferred_users", "in", this.user]]
        const records = await this.orm.call("connect.call", "get_widget_calls", [domain, 20])
        for (const item of records) {
            // Debug ALL calls to see call_pattern values
            console.log('ALL CALLS DEBUG:', item.id, {
                call_pattern: item.call_pattern,
                direction: item.direction,
                status: item.status,
                transferred_users: item.transferred_users?.length || 0
            });
            
            // For incoming calls, always use caller (external party) for callback/favorites
            // For outgoing calls, use called (who we called)
            const call_number = item.direction === 'incoming' ? item.caller : item.called
            item.favorite = this.favorites.includes(call_number)
            
            // Format date as MM/DD/YY H:MM AM/PM (user's local timezone)
            const call_date = new Date(`${item.create_date} UTC`)
            const month = String(call_date.getMonth() + 1).padStart(2, '0')
            const day = String(call_date.getDate()).padStart(2, '0')
            const year = String(call_date.getFullYear()).slice(-2)
            let hours = call_date.getHours()
            const minutes = String(call_date.getMinutes()).padStart(2, '0')
            const ampm = hours >= 12 ? 'PM' : 'AM'
            hours = hours % 12
            hours = hours ? hours : 12 // 0 should be 12
            item.create_date = `${month}/${day}/${year} ${hours}:${minutes} ${ampm}`
            
            // Use backend notification logic for red highlighting
            item.is_missed = (
                item.notification_user_ids && 
                item.notification_user_ids.includes(this.user)
            )
            
            // Check if current user received transfer on this call
            item.user_received_transfer = (
                item.transferred_users && 
                item.transferred_users.some(user_id => parseInt(user_id) === parseInt(this.user))
            )
            
            // Debug outgoing transfers
            if (item.direction === 'outgoing' && item.user_received_transfer) {
                console.log('OUTGOING TRANSFER DEBUG:', item.id, {
                    direction: item.direction,
                    user_received_transfer: item.user_received_transfer,
                    transferred_users: item.transferred_users,
                    caller_user: item.caller_user,
                    called: item.called,
                    called_users: item.called_users,
                    current_user: this.user
                });
            }
            
            // Determine if this user received a transfer
            // For ring group calls, transferred_users may contain all ring participants
            // A true transfer recipient is someone who:
            // 1. Is in transferred_users AND
            // 2. Was NOT in the original called_users (original ring group)
            const was_originally_called = item.called_users && item.called_users.some(user_id => 
                parseInt(user_id) === parseInt(this.user)  // Ensure both are same data type
            );
            item.is_transfer_recipient = (
                item.transferred_users && 
                item.transferred_users.length > 0 && 
                item.transferred_users.includes(this.user) &&
                !was_originally_called  // Only true transfer recipients, not original ring group members
            )
            
            // Debug logging for call patterns
            console.log('Call pattern debug:', item.id, {
                call_pattern: item.call_pattern,
                direction: item.direction,
                transferred_users: item.transferred_users?.length || 0
            });
            
            // Debug logging for transfer scenarios only
            if (item.transferred_users && item.transferred_users.length > 0) {
                console.log('Transfer call debug:', item.id, {
                    transferred_users: item.transferred_users,
                    called_users: item.called_users,
                    is_transfer_recipient: item.is_transfer_recipient,
                    was_originally_called: was_originally_called
                });
            }
            
            // For transfer recipients, we want to show the original caller info
            // instead of the transferring user's info
            if (item.is_transfer_recipient) {
                // The original caller info should be in item.caller/item.caller_user
                // The transferring user info would be in answered_user
                item.display_caller_info = {
                    is_transfer: true,
                    original_caller: item.caller,
                    original_caller_user: item.caller_user,
                    original_partner: item.partner,
                    transferring_user: item.answered_user
                }
                console.log('Transfer recipient display info:', item.id, item.display_caller_info);
            } else {
                item.display_caller_info = {
                    is_transfer: false
                }
            }
        }
        this.state.calls = records

    }

    async _getFavorites() {
        this.favorites = []
        const favorites = await this.orm.searchRead('connect.favorite', [], ['phone_number'])
        favorites.forEach((el) => this.favorites.push(el.phone_number))
        this.state.calls.forEach(item => {
            const call_number = item.called_users[0] === this.user ? item.caller : item.called
            item.favorite = this.favorites.includes(call_number)
        })
    }

    _onClickContactCall(phoneNumber) {
        this.bus.trigger('busPhoneMakeCall', {phone: phoneNumber})
    }

    async _onClickFavorite(call) {
        const kwargs = {}
        const isCalled = call.called_users[0] === this.user
        kwargs.phone_number = isCalled ? call.caller : call.called
        if (call.partner) {
            kwargs.partner = call.partner[0]
        } else {
            if (call.caller_user && isCalled) {
                kwargs.user = call.caller_user[0]
            } else if (call.called_users.length > 0 && !isCalled) {
                kwargs.user = call.called_users[0]
            } else {
                kwargs.name = kwargs.phone_number
            }
        }

        const domain = [["phone_number", "=", kwargs.phone_number]]
        const getFavorite = await this.orm.search('connect.favorite', domain)

        if (getFavorite.length === 0) {
            await this.orm.create('connect.favorite', [kwargs])
            await this._getFavorites()
            this.notification.add('Added to Favorite!', {title: 'Phone', type: 'info'})
        } else {
            await this.orm.unlink("connect.favorite", getFavorite, {})
            await this._getFavorites()
            this.notification.add('Removed from Favorite!', {title: 'Phone', type: 'info'})
        }
    }

    _open_detail(call) {
        this.state.call = call
    }

    _close_call_detail() {
        this.state.call = null
        this._getCalls()
    }
}
