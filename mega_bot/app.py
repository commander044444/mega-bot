from __future__ import annotations
import asyncio, json, logging, random, re, secrets, string
from datetime import datetime, timezone
from typing import Any
from .bale_api import BaleAPI, BaleAPIError
from .database import Database
from .keyboards import USER_KEYBOARD, inline, menu_page, main_admin_menu, user_actions
from config import OWNER_ID, BOT_VERSION, SUPPORT_TEXT, DONATION_TEXT

log = logging.getLogger(__name__)

DEFAULT_PERMS = {
    'stats': True, 'search': True, 'case_lookup': True,
    'unban_user': True, 'unban_group': True,
    'ban_user': False, 'ban_group': False,
    'edit_about': False, 'edit_team': False,
    'manage_admins': False, 'settings': False,
}
PERMISSION_LABELS = {
    'stats':'مشاهده آمار', 'search':'جست‌وجوی کاربران و گروه‌ها', 'case_lookup':'بررسی کد پیگیری',
    'unban_user':'رفع محدودیت کاربر', 'unban_group':'رفع محدودیت گروه',
    'ban_user':'محدودسازی کاربر', 'ban_group':'محدودسازی گروه', 'edit_about':'ویرایش درباره ما',
    'edit_team':'مدیریت تیم توسعه‌دهنده', 'manage_admins':'مدیریت ادمین‌ها', 'settings':'تنظیمات ربات'
}

class MegaBot:
    def __init__(self, api: BaleAPI, db: Database):
        self.api, self.db = api, db
        self.offset: int | None = None
        self.states: dict[int, dict[str, Any]] = {}
        self.game_cache: dict[str, dict[str, Any]] = {}

    async def safe_send(self, chat_id: int, text: str, keyboard: dict | None = None, reply_to: int | None = None, parse_mode: str | None = 'Markdown') -> dict | None:
        try:
            return await self.api.send_message(chat_id, text, keyboard, reply_to, parse_mode)
        except Exception as exc:
            log.warning('sendMessage failed for chat %s: %s', chat_id, exc)
            return None

    async def safe_edit(self, chat_id: int, message_id: int, text: str, keyboard: dict | None = None) -> None:
        try:
            await self.api.edit_message(chat_id, message_id, text, keyboard)
        except Exception as exc:
            log.warning('editMessageText failed: %s', exc)
            await self.safe_send(chat_id, text, keyboard)

    async def run(self, poll_timeout: int = 25) -> None:
        log.info('MEGA BOT polling started')
        while True:
            try:
                updates = await self.api.get_updates(self.offset, poll_timeout)
                for update in updates:
                    self.offset = int(update.get('update_id', 0)) + 1
                    try:
                        if 'message' in update:
                            await self.on_message(update['message'])
                        elif 'callback_query' in update:
                            await self.on_callback(update['callback_query'])
                    except Exception:
                        log.exception('Failed to process update %s', update.get('update_id'))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Polling failed; retrying shortly')
                await asyncio.sleep(3)

    async def on_message(self, msg: dict) -> None:
        chat = msg.get('chat') or {}
        user = msg.get('from') or msg.get('sender') or {}
        uid = int(user.get('id', 0) or 0)
        chat_id = int(chat.get('id', 0) or 0)
        if not uid or not chat_id:
            return
        text = (msg.get('text') or '').strip()
        await self.db.upsert_user(user)
        if chat.get('type') in ('group', 'supergroup'):
            await self.db.upsert_group(chat)
        await self.db.record_usage(uid, chat, 'message')

        # User restrictions apply in private and group chats, while keeping case tracking accessible.
        restricted_user = await self.db.is_restricted('user', uid)
        if restricted_user and uid != OWNER_ID and not await self.db.is_admin(uid) and text not in ('تیم من',) and not text.startswith('/admin') and not text.startswith('/owner'):
            await self.safe_send(chat_id, f"*⛔ دسترسی تو به قابلیت‌های MEGA BOT محدود شده است.*\nکد پیگیری: `#{restricted_user['case_code']}`\nبرای بررسی دلیل و مدت، بخش *تیم من* را باز کن.", USER_KEYBOARD)
            return

        # Keep group-level restriction enforced for bot features.
        if chat.get('type') in ('group', 'supergroup'):
            restricted_group = await self.db.is_restricted('group', chat_id)
            feature_request = text.startswith('/') or text in ('دوز','بازی دوز','شیر یا خط','قوانین','پین','لیست ویژه ها','خوبی','چخبر','خدافظ','شب خوش','سلام ربات') or text.startswith(('اکو ','تنظیم قوانین ','هرکی تک')) or ('ربات' in text.lower())
            if restricted_group and feature_request and not self._is_admin_identity(uid):
                await self.safe_send(chat_id, f"*⛔ این گروه توسط مدیریت محدود شده است.*\nکد پیگیری: `#{restricted_group['case_code']}`", reply_to=msg.get('message_id'))
                return
        if msg.get('new_chat_members'):
            await self.welcome_new_members(msg, chat)
            return

        if text.startswith('/start'):
            await self.safe_send(chat_id, '*سلام! به MEGA BOT خوش اومدی* 🌿\nاز منوی پایین برای دیدن بخش‌ها استفاده کن.', USER_KEYBOARD)
            return
        if text.startswith('/admin'):
            if uid != OWNER_ID and not await self.db.is_admin(uid):
                await self.safe_send(chat_id, '⛔ دسترسی به پنل مدیریت نداری.')
                return
            await self.show_admin(chat_id, uid, owner=(uid == OWNER_ID))
            return
        if text.startswith('/owner'):
            if uid != OWNER_ID:
                await self.safe_send(chat_id, '⛔ این پنل فقط برای مالک ربات است.')
                return
            await self.show_admin(chat_id, uid, owner=True)
            return
        # If an admin/user is in a multi-step flow, consume their next text before menu shortcuts.
        state = self.states.get(uid)
        if state and int(state.get('chat_id', chat_id)) == chat_id:
            handled = await self.handle_state(chat_id, uid, user, text, state, msg)
            if handled:
                return

        if text == 'درباره ما':
            about = await self.db.get_setting('about', 'MEGA BOT یک ربات اجتماعی برای بله است.')
            await self.safe_send(chat_id, f'*درباره MEGA BOT*\n\n{about}', USER_KEYBOARD)
            return
        if text == 'راهنما ربات':
            await self.show_guide(chat_id, 0)
            return
        if text == 'منوی بیشتر':
            await self.safe_send(chat_id, '*بخش‌های بیشتر*', inline([
                [('🆘 پشتیبانی','more:support'),('💚 حمایت مالی','more:donate')],
                [('👨‍💻 تیم توسعه','more:team'),('📊 آمار و اطلاعات','more:stats')],
                [('↩️ برگشت','home'),('✖️ بستن','close')]
            ]))
            return
        if text == 'تیم من':
            await self.my_cases(chat_id, uid)
            return
        if text == 'پین' and msg.get('reply_to_message'):
            if not (await self._is_chat_admin(chat_id, uid) or uid == OWNER_ID):
                await self.safe_send(chat_id, '⛔ فقط ادمین گروه می‌تواند پیام را پین کند.')
                return
            try:
                await self.api.pin_message(chat_id, int(msg['reply_to_message']['message_id']))
                await self.safe_send(chat_id, '📌 پیام پین شد.')
            except Exception as exc:
                await self.safe_send(chat_id, f'نتونستم پیام رو پین کنم؛ دسترسی ربات رو بررسی کن.\n`{self._short(exc)}`')
            return
        if text.startswith('تنظیم قوانین '):
            if chat.get('type') not in ('group','supergroup') or not await self._is_chat_admin(chat_id, uid):
                await self.safe_send(chat_id, 'فقط ادمین گروه می‌تواند قوانین را تنظیم کند.')
                return
            await self.db.set_setting(f'rules:{chat_id}', text[len('تنظیم قوانین '):].strip(), uid)
            await self.safe_send(chat_id, '✅ قوانین گروه ذخیره شد.')
            return
        if text == 'قوانین' and chat.get('type') in ('group','supergroup'):
            await self.safe_send(chat_id, 'میخوای قوانین این گروه رو ببینی؟', inline([[('بله','rules:yes'),('خیر','rules:no')], [('✖️ بستن','close')]]))
            return
        if text.startswith('اکو '):
            echo = text[4:].strip()
            try:
                await self.api.delete_message(chat_id, int(msg['message_id']))
            except Exception:
                pass
            if echo:
                await self.safe_send(chat_id, echo)
            return
        if text.lower() in ('دوز','بازی دوز','دوز بازی') and chat.get('type') in ('group','supergroup'):
            await self.start_ttt(chat_id, uid, user.get('first_name') or 'بازیکن')
            return
        if re.fullmatch(r'شیر\s*یا\s*خط', text):
            game_id = secrets.token_hex(3)
            self.game_cache[game_id] = {'kind':'coin','creator':uid,'chat_id':chat_id,'players':{}}
            await self.safe_send(chat_id, '*بازی شیر یا خط*\nهر دو بازیکن انتخابشون رو ثبت کنن.', inline([
                [('🦁 شیر','coin:choose:'+game_id+':heads'),('🪙 خط','coin:choose:'+game_id+':tails')],
                [('✖️ لغو بازی','game:cancel:'+game_id)]
            ]))
            return
        if re.fullmatch(r'هرکی تک بیاره+', text) or re.fullmatch(r'هرکی تک+ بیاره+', text):
            await self.start_one_game(chat_id, uid, user.get('first_name') or 'بازیکن')
            return
        if chat.get('type') in ('group','supergroup') and await self._is_chat_admin(chat_id, uid) and text == 'لیست ویژه ها':
            await self.safe_send(chat_id, 'فهرست ویژه‌ها در این نسخه از پنل مالک مدیریت می‌شود؛ بخش دسترسی‌ها را باز کن.', inline([[('🛡️ پنل مالک','owner:open')], [('↩️ برگشت','home'),('✖️ بستن','close')]]))
            return

        if chat.get('type') in ('group','supergroup'):
            reply = self.social_reply(text, await self.db.stats())
            if reply:
                await self.safe_send(chat_id, reply, reply_to=msg.get('message_id'))

    async def on_callback(self, query: dict) -> None:
        callback_id = query.get('id')
        data = query.get('data', '')
        user = query.get('from') or {}
        uid = int(user.get('id', 0) or 0)
        message = query.get('message') or {}
        chat = message.get('chat') or {}
        chat_id = int(chat.get('id', 0) or 0)
        message_id = int(message.get('message_id', 0) or 0)
        try:
            await self.api.answer_callback(callback_id)
        except Exception:
            pass
        if data == 'close':
            self.states.pop(uid, None)
            await self.safe_edit(chat_id, message_id, 'این منو بسته شد. برای بازکردن دوباره از منوی اصلی استفاده کن.', None)
            return
        if data == 'home':
            await self.safe_edit(chat_id, message_id, '*منوی اصلی MEGA BOT*', None)
            return
        if chat.get('type') in ('group', 'supergroup') and uid != OWNER_ID and data.startswith(('ttt:', 'one:', 'coin:', 'rules:')):
            restricted = await self.db.is_restricted('group', chat_id)
            if restricted:
                await self.safe_send(chat_id, f"*⛔ این گروه توسط مدیریت محدود شده است.*\nکد پیگیری: `#{restricted['case_code']}`")
                return
        if data.startswith('more:'):
            key = data.split(':',1)[1]
            if key == 'support': await self.safe_edit(chat_id,message_id,'*پشتیبانی*\n'+SUPPORT_TEXT, inline([[('↩️ برگشت','home'),('✖️ بستن','close')]]))
            elif key == 'donate': await self.safe_edit(chat_id,message_id,'*حمایت مالی*\n'+DONATION_TEXT, inline([[('↩️ برگشت','home'),('✖️ بستن','close')]]))
            elif key == 'team':
                team = await self.db.get_team()
                text = '*تیم توسعه‌دهنده*\n\n' + ('\n'.join(f"*{m['name']}* — {m['title']}\n{m['description']}" for m in team) if team else 'هنوز عضوی ثبت نشده است.')
                await self.safe_edit(chat_id,message_id,text,inline([[('↩️ برگشت','home'),('✖️ بستن','close')]]))
            elif key == 'stats':
                s = await self.db.stats()
                await self.safe_edit(chat_id,message_id,self.stats_text(s),inline([[('↩️ برگشت','home'),('✖️ بستن','close')]]))
            return
        if data.startswith('guide:item:'):
            index = int(data.rsplit(':', 1)[1])
            guide_items = [
                ('👤 پروفایل', 'پروفایل و اطلاعات پایه'), ('🧩 تعامل اجتماعی', 'پاسخ‌های متنوع به گفت‌وگو'),
                ('🎮 بازی دوز', 'شروع بازی دوز در گروه'), ('🪙 شیر یا خط', 'بازی دونفره شیر یا خط'),
                ('🎲 هرکی تک بیاره', 'بازی گروهی هرکی تک بیاره'), ('📌 پین پیام', 'ریپلای به پیام و نوشتن پین'),
                ('📜 قوانین گروه', 'تنظیم قوانین توسط ادمین گروه'), ('🛡️ مدیریت', 'پنل‌های مدیریتی و سطح دسترسی'),
                ('🎫 پیگیری بن', 'وارد کردن کد چهارتایی در پنل'), ('ℹ️ درباره ما', 'نمایش اطلاعات MEGA BOT')]
            if 0 <= index < len(guide_items):
                label, desc = guide_items[index]
                await self.safe_edit(chat_id, message_id, f'*{label}*\n{desc}', inline([[('↩️ برگشت', f'guide:page:{index//2}'), ('✖️ بستن', 'close')]]))
            return
        if data.startswith('guide:page:'):
            await self.show_guide(chat_id, int(data.rsplit(':',1)[1]), message_id)
            return
        if data.startswith('rules:'):
            if data == 'rules:no':
                await self.safe_edit(chat_id,message_id,'باشه، هر وقت خواستی می‌تونی دوباره «قوانین» رو بزنی.', None)
            else:
                rules = await self.db.get_setting(f'rules:{chat_id}')
                try:
                    await self.api.send_message(uid, f'*قوانین گروه*\n\n{rules or "برای این گروه قانونی ثبت نشده است."}', inline([[('✖️ بستن','close')]]))
                    await self.safe_edit(chat_id,message_id,'قوانین در خصوصی برات ارسال شد.', None)
                except Exception:
                    await self.safe_edit(chat_id,message_id,'برای ارسال قوانین در خصوصی، اول ربات رو در گفت‌وگوی خصوصی Start کن.', None)
            return
        if data.startswith('coin:choose:'):
            _,_,game_id,choice = data.split(':',3)
            game = self.game_cache.get(game_id)
            if not game or game.get('kind') != 'coin':
                await self.safe_send(chat_id,'این بازی دیگه فعال نیست.')
                return
            game['players'][uid] = choice
            if len(game['players']) < 2:
                await self.safe_edit(chat_id,message_id,f"*شیر یا خط*\nانتخاب {len(game['players'])}/2 ثبت شد؛ نفر دوم انتخاب کنه.", inline([[('🦁 شیر','coin:choose:'+game_id+':heads'),('🪙 خط','coin:choose:'+game_id+':tails')],[('✖️ لغو بازی','game:cancel:'+game_id)]]))
            else:
                choices = list(game['players'].items())
                winner = random.choice(['heads','tails'])
                labels = {'heads':'شیر','tails':'خط'}
                results = '\n'.join(f"کاربر `{pid}`: {labels[ch]}" for pid,ch in choices)
                result = f"*نتیجه شیر یا خط:* {labels[winner]}\n{results}\n" + ('🎉 برنده مشخص شد!' if any(ch == winner for _,ch in choices) else 'این دور برنده‌ای نداشت.')
                await self.safe_edit(chat_id,message_id,result,inline([[('✖️ بستن','close')]]))
                self.game_cache.pop(game_id,None)
            return
        if data.startswith('game:cancel:'):
            gid = data.rsplit(':',1)[1]
            game = self.game_cache.get(gid)
            if game and int(game.get('creator',-1)) != uid:
                await self.safe_send(chat_id,'فقط سازنده می‌تواند بازی را لغو کند.')
            else:
                self.game_cache.pop(gid,None)
                await self.safe_edit(chat_id,message_id,'بازی لغو شد.',None)
            return
        if data.startswith('ttt:'):
            await self.handle_ttt_callback(query, data)
            return
        if data.startswith('one:'):
            await self.handle_one_callback(query, data)
            return
        if data == 'owner:open':
            if uid == OWNER_ID: await self.show_admin(chat_id,uid,True,message_id)
            return
        if data.startswith('admin:page:'):
            if uid == OWNER_ID or await self.db.is_admin(uid):
                await self.show_admin(chat_id,uid,uid==OWNER_ID,message_id,int(data.rsplit(':',1)[1]))
            return
        if data.startswith('adm:target:'):
            await self.handle_target_select(uid, chat_id, message_id, data)
            return
        if data.startswith('adm:') or data.startswith('own:') or data.startswith('mod:') or data.startswith('team:'):
            await self.handle_admin_callback(uid, chat_id, message_id, data)
            return
        if data.startswith('perm:'):
            await self.handle_permission_callback(uid, chat_id, message_id, data)
            return

    def _is_admin_identity(self, uid: int) -> bool:
        return uid == OWNER_ID

    async def _is_chat_admin(self, chat_id: int, uid: int) -> bool:
        try:
            result = await self.api.call('getChatMember', {'chat_id':chat_id,'user_id':uid})
            return (result.get('status') in ('creator','administrator'))
        except Exception:
            return False

    @staticmethod
    def _short(exc: Exception) -> str:
        return str(exc).replace('\n',' ')[:180]

    def social_reply(self, text: str, stats: dict) -> str | None:
        low = text.lower().strip().replace('؟','').replace('!','')
        responses = {
            'سلام ربات':['سلام🍃 چخبر؟','سلام سلام 😄 خوبی؟','درود بهت 🌿 چه خبرا؟'],
            'سلام':['سلاممم 🍃','سلام رفیق 😄','سلام، خوش اومدی 🌱'],
            'شب خوش':['شب تو هم خوش 😊','خوابای قشنگ ببینی 🌙','شب آرومی داشته باشی 🍃'],
            'خوبی':['مرسی، خوبم 😄 تو چطوری؟','خوبم، تو چه خبر؟ 🌿','دارم به پیام‌ها می‌رسم 😊'],
            'چخبر':[f"هیچی فقط دارم به {stats.get('groups',0)} گروه رسیدگی میکنم⛱️",'خبر خاصی نیست، تو بگو 🌱','همه‌چی آرومه فعلاً 🍃'],
            'خدافظ':['فعلاً، مراقب خودت باش 🌿','بای بای 😄','به امید دیدار 🍃'],
        }
        for key, values in responses.items():
            if low == key or (key == 'سلام' and low.startswith('سلام')):
                return random.choice(values)
        return None

    async def welcome_new_members(self, msg: dict, chat: dict) -> None:
        members = msg.get('new_chat_members') or []
        rules = await self.db.get_setting(f"rules:{chat.get('id')}")
        for member in members[:10]:
            await self.db.upsert_user(member)
            name = member.get('first_name') or 'دوست'
            uid = member.get('id')
            text = f"سلام [{name}](uid:{uid}) عزیز⛱️\nخیلی خوش اومدی به گروه *{chat.get('title','گروه')}*\nفقط یادت باشه اگه تونستی یه سر به قوانین گروه بزنی ✨️"
            keyboard = inline([[('📜 قوانین گروه','rules:show')]]) if rules else None
            await self.safe_send(int(chat['id']), text, keyboard)

    async def show_guide(self, chat_id: int, page: int, edit_id: int | None = None) -> None:
        entries = [
            ('👤 پروفایل','راهنما: پروفایل و اطلاعات پایه'),('🧩 تعامل اجتماعی','راهنما: پاسخ‌های متنوع به گفت‌وگو'),
            ('🎮 بازی دوز','راهنما: شروع بازی دوز در گروه'),('🪙 شیر یا خط','راهنما: بازی دونفره شیر یا خط'),
            ('🎲 هرکی تک بیاره','راهنما: بازی گروهی هرکی تک بیاره'),('📌 پین پیام','راهنما: ریپلای به پیام و نوشتن پین'),
            ('📜 قوانین گروه','راهنما: تنظیم قوانین توسط ادمین گروه'),('🛡️ مدیریت','راهنما: پنل‌های مدیریتی و سطح دسترسی'),
            ('🎫 پیگیری بن','راهنما: وارد کردن کد چهارتایی در پنل'),('ℹ️ درباره ما','راهنما: نمایش اطلاعات MEGA BOT'),
        ]
        start = page*2
        text = f'*راهنمای MEGA BOT* — صفحه {page+1}\nیک بخش رو انتخاب کن تا توضیحش رو ببینی.'
        rows = [[(label,'guide:item:'+str(start+i))] for i,(label,_) in enumerate(entries[start:start+2])]
        controls = []
        if page > 0: controls.append(('⬅️ قبلی',f'guide:page:{page-1}'))
        if start+2 < len(entries): controls.append(('بعدی ➡️',f'guide:page:{page+1}'))
        if controls: rows.append(controls)
        rows.append([('↩️ برگشت','home'),('✖️ بستن','close')])
        kb = inline(rows)
        if edit_id: await self.safe_edit(chat_id,edit_id,text,kb)
        else: await self.safe_send(chat_id,text,kb)

    def stats_text(self, s: dict) -> str:
        return (f"*📊 آمار MEGA BOT*\n\n*👤 کل کاربران:* {s['users']}\n*🆕 کاربران جدید امروز:* {s['new_today']}\n"
                f"*👥 گروه‌های ثبت‌شده:* {s['groups']}\n*🙋 کاربران استفاده‌کننده در گروه‌ها:* {s['group_users']}\n"
                f"*⛔ محدودیت فعال کاربران:* {s['banned_users']}\n*🚫 گروه‌های محدود:* {s['banned_groups']}\n"
                f"*💚 مجموع حمایت مالی:* {s['donations']} تومان\n\n*ℹ️ نسخه {BOT_VERSION}*")

    async def show_admin(self, chat_id: int, uid: int, owner: bool, edit_id: int | None = None, page: int = 0) -> None:
        if not owner:
            admin = await self.db.get_admin(uid)
            perms = admin['permissions'] if admin else {}
            items = []
            if perms.get('stats'): items.append(('📊 آمار و گزارش‌ها','adm:stats'))
            if perms.get('search'): items += [('👤 جست‌وجوی کاربر','adm:search:user'),('👥 جست‌وجوی گروه','adm:search:group')]
            if perms.get('case_lookup'): items.append(('🎫 بررسی کد پیگیری','adm:case'))
            if perms.get('ban_user') or perms.get('unban_user'): items.append(('👤 محدودیت کاربران','adm:search:user'))
            if perms.get('ban_group') or perms.get('unban_group'): items.append(('👥 محدودیت گروه‌ها','adm:search:group'))
            if perms.get('edit_about'): items.append(('📝 ویرایش درباره ما','own:about'))
            if perms.get('edit_team'): items.append(('👨‍💻 تیم توسعه','own:team'))
            if not items: items = [('📊 آمار و گزارش‌ها','adm:stats')]
        else:
            items = [
                ('📊 آمار و گزارش‌ها','adm:stats'),('👤 جست‌وجوی کاربر','adm:search:user'),('👥 جست‌وجوی گروه','adm:search:group'),
                ('🎫 بررسی کد پیگیری','adm:case'),('🧾 گزارش فعالیت‌ها','adm:logs'),('🛡️ مدیریت ادمین‌ها','own:admins'),
                ('📝 ویرایش درباره ما','own:about'),('👨‍💻 تیم توسعه‌دهنده','own:team'),('⚙️ تنظیمات مالک','own:settings')]
        # Exactly three content items per page, plus optional previous/next and back/close.
        start = page*2
        rows = [[(label,data)] for label,data in items[start:start+2]]
        controls=[]
        if page>0: controls.append(('⬅️ قبلی',f'admin:page:{page-1}'))
        if start+2<len(items): controls.append(('بعدی ➡️',f'admin:page:{page+1}'))
        if controls: rows.append(controls)
        rows.append([('↩️ برگشت','home'),('✖️ بستن','close')])
        text = '*پنل مالک MEGA BOT*\nیک بخش رو انتخاب کن.' if owner else '*پنل ادمین MEGA BOT*\nدسترسی‌ها بر اساس مجوزهای تو نمایش داده می‌شوند.'
        if edit_id: await self.safe_edit(chat_id,edit_id,text,inline(rows))
        else: await self.safe_send(chat_id,text,inline(rows))

    async def has_perm(self, uid: int, perm: str) -> bool:
        if uid == OWNER_ID: return True
        admin = await self.db.get_admin(uid)
        return bool(admin and (admin.get('permissions') or {}).get(perm, False))

    async def handle_admin_callback(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        owner = uid == OWNER_ID
        if not owner and not await self.db.is_admin(uid):
            await self.safe_send(chat_id,'⛔ دسترسی مدیریت نداری.')
            return
        if data == 'adm:stats':
            if not await self.has_perm(uid,'stats'): return await self.safe_send(chat_id,'⛔ مجوز مشاهده آمار نداری.')
            await self.safe_edit(chat_id,message_id,self.stats_text(await self.db.stats()),inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        if data in ('adm:search:user','adm:search:group'):
            if not await self.has_perm(uid,'search'): return await self.safe_send(chat_id,'⛔ مجوز جست‌وجو نداری.')
            target_type = data.rsplit(':',1)[1]
            self.states[uid]={'step':'search','target_type':target_type,'chat_id':chat_id,'message_id':message_id}
            await self.safe_edit(chat_id,message_id,f"*جست‌وجوی {'کاربر' if target_type=='user' else 'گروه'}*\nشناسه عددی یا `@username` رو در همین گفتگو بفرست.",inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        if data == 'adm:case':
            if not await self.has_perm(uid,'case_lookup'): return await self.safe_send(chat_id,'⛔ مجوز بررسی پرونده نداری.')
            self.states[uid]={'step':'case_lookup','chat_id':chat_id,'message_id':message_id}
            await self.safe_edit(chat_id,message_id,'کد پیگیری رو بفرست؛ مثال: `#5453`',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        if data == 'adm:logs':
            if not owner and not await self.has_perm(uid,'settings'): return await self.safe_send(chat_id,'⛔ مشاهده گزارش فعالیت‌ها فقط برای مالک یا ادمین مجاز است.')
            async with self.db._pool().acquire() as c:
                rows = await c.fetch('SELECT actor_id,action,target_type,target_id,created_at FROM audit_logs ORDER BY id DESC LIMIT 10')
            body = '*آخرین فعالیت‌ها*\n\n' + ('\n'.join(f"• `{r['actor_id']}` — {r['action']} — {r['target_type'] or '-'} `{r['target_id'] or '-'}`" for r in rows) if rows else 'هنوز فعالیتی ثبت نشده.')
            await self.safe_edit(chat_id,message_id,body,inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        if data == 'own:about':
            if not await self.has_perm(uid,'edit_about'): return await self.safe_send(chat_id,'⛔ مجوز ویرایش درباره ما نداری.')
            self.states[uid]={'step':'edit_about','chat_id':chat_id,'message_id':message_id}
            current = await self.db.get_setting('about','')
            await self.safe_edit(chat_id,message_id,f"*ویرایش درباره ما*\nمتن فعلی:\n{current}\n\nحالا متن جدید رو در همین گفتگو بفرست.",inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        if data == 'own:team':
            if not await self.has_perm(uid,'edit_team'): return await self.safe_send(chat_id,'⛔ مجوز مدیریت تیم نداری.')
            team = await self.db.get_team()
            rows = [[('➕ افزودن عضو','team:add')]]
            rows += [[(f"👤 {m['name'][:25]}",f"team:member:{m['id']}")] for m in team[:3]]
            rows.append([('↩️ برگشت','admin:page:0'),('✖️ بستن','close')])
            text='*مدیریت تیم توسعه‌دهنده*\n\n'+('\n'.join(f"`{m['id']}`. *{m['name']}* — {m['title']}" for m in team) if team else 'اعضای تیم هنوز ثبت نشده‌اند.')
            await self.safe_edit(chat_id,message_id,text,inline(rows)); return
        if data.startswith('team:member:'):
            if not await self.has_perm(uid,'edit_team'): return
            member_id=int(data.rsplit(':',1)[1])
            team=await self.db.get_team(); member=next((m for m in team if int(m['id'])==member_id),None)
            if not member: return await self.safe_send(chat_id,'عضو تیم پیدا نشد.')
            await self.safe_edit(chat_id,message_id,f"*{member['name']}*\nعنوان: {member['title']}\nتوضیح: {member['description'] or 'ندارد'}\nآیدی: @{member['username'] or 'ندارد'}",inline([[('✏️ ویرایش عضو',f'team:edit:{member_id}')],[('🗑️ حذف عضو',f'team:del:{member_id}')],[('↩️ برگشت','own:team'),('✖️ بستن','close')]])); return
        if data.startswith('team:edit:'):
            if not await self.has_perm(uid,'edit_team'): return
            member_id=int(data.rsplit(':',1)[1])
            team=await self.db.get_team(); member=next((m for m in team if int(m['id'])==member_id),None)
            if not member: return await self.safe_send(chat_id,'عضو تیم پیدا نشد.')
            self.states[uid]={'step':'team_edit_name','chat_id':chat_id,'message_id':message_id,'member_id':member_id,'team_data':{}}
            await self.safe_send(chat_id,f"نام جدید عضو را بفرست. نام فعلی: {member['name']}")
            return
        if data == 'team:add':
            if not await self.has_perm(uid,'edit_team'): return
            self.states[uid]={'step':'team_add_name','chat_id':chat_id,'message_id':message_id,'team_data':{}}
            await self.safe_edit(chat_id,message_id,'نام عضو جدید تیم را بفرست.',inline([[('↩️ برگشت','own:team'),('✖️ بستن','close')]])); return
        if data.startswith('team:del:'):
            if not await self.has_perm(uid,'edit_team'): return
            member_id=int(data.rsplit(':',1)[1])
            ok=await self.db.delete_team_member(member_id,uid)
            await self.safe_edit(chat_id,message_id,'عضو حذف شد.' if ok else 'این عضو پیدا نشد.',inline([[('↩️ برگشت','own:team'),('✖️ بستن','close')]])); return
        if data == 'own:admins':
            if not owner: return await self.safe_send(chat_id,'⛔ فقط مالک می‌تواند ادمین‌ها را مدیریت کند.')
            admins=await self.db.list_admins(10,0)
            rows=[[('➕ افزودن ادمین','own:admin_add')]]
            for a in admins[:2]: rows.append([(f"⚙️ {a['display_name'] or a['user_id']}",f"own:admin:{a['user_id']}")])
            rows.append([('↩️ برگشت','admin:page:0'),('✖️ بستن','close')])
            text='*مدیریت ادمین‌ها*\n'+('\n'.join(f"• `{a['user_id']}` — {'فعال' if a['active'] else 'غیرفعال'}" for a in admins) if admins else 'هنوز ادمینی ثبت نشده.')
            await self.safe_edit(chat_id,message_id,text,inline(rows)); return
        if data == 'own:admin_add':
            if not owner: return
            self.states[uid]={'step':'admin_add_id','chat_id':chat_id,'message_id':message_id}
            await self.safe_edit(chat_id,message_id,'شناسه عددی ادمین جدید رو بفرست.',inline([[('↩️ برگشت','own:admins'),('✖️ بستن','close')]])); return
        if data.startswith('own:admin:'):
            if not owner: return
            target=int(data.rsplit(':',1)[1])
            async with self.db._pool().acquire() as c:
                row=await c.fetchrow('SELECT * FROM admins WHERE user_id=$1',target)
            if not row: return await self.safe_send(chat_id,'ادمین پیدا نشد.')
            perms=row['permissions'] or {}
            rows=[[('🗑️ حذف ادمین',f'own:admin_del:{target}')]]
            for key,label in list(PERMISSION_LABELS.items())[:2]: rows.append([( ('✅ ' if perms.get(key) else '▫️ ')+label, f'perm:toggle:{target}:{key}')])
            rows.append([('بعدی ➡️',f'own:admin_perms:{target}:1')])
            rows.append([('↩️ برگشت','own:admins'),('✖️ بستن','close')])
            await self.safe_edit(chat_id,message_id,f"*تنظیم ادمین* `{target}`\nروی هر مجوز بزن تا روشن/خاموش شود.",inline(rows)); return
        if data.startswith('own:admin_perms:'):
            if not owner: return
            _,_,target,page=data.split(':')
            target=int(target); page=int(page)
            keys=list(PERMISSION_LABELS.items()); chunk=keys[page*2:(page+1)*2]
            async with self.db._pool().acquire() as c: perms=await c.fetchval('SELECT permissions FROM admins WHERE user_id=$1',target) or {}
            rows=[[(('✅ ' if perms.get(k) else '▫️ ')+label,f'perm:toggle:{target}:{k}')] for k,label in chunk]
            if page>0: rows.append([('⬅️ قبلی',f'own:admin_perms:{target}:{page-1}')])
            if (page+1)*2 < len(keys): rows.append([('بعدی ➡️',f'own:admin_perms:{target}:{page+1}')])
            rows.append([('↩️ برگشت',f'own:admin:{target}'),('✖️ بستن','close')])
            await self.safe_edit(chat_id,message_id,f'*دسترسی‌های ادمین* `{target}`',inline(rows)); return
        if data.startswith('own:admin_del:'):
            if not owner: return
            target=int(data.rsplit(':',1)[1])
            async with self.db._pool().acquire() as c: await c.execute('DELETE FROM admins WHERE user_id=$1',target)
            await self.db.log(uid,'admin_removed','user',target)
            await self.safe_edit(chat_id,message_id,'ادمین حذف شد.',inline([[('↩️ برگشت','own:admins'),('✖️ بستن','close')]])); return
        if data == 'own:settings':
            if not owner: return
            await self.safe_edit(chat_id,message_id,'*تنظیمات مالک*\nنسخه: '+BOT_VERSION+'\nشناسه مالک: `'+str(OWNER_ID)+'`\n\nتنظیمات محرمانه را در config.py یا متغیرهای محیطی Railway تغییر بده.',inline([[('👨‍💻 تیم توسعه','own:team'),('📝 درباره ما','own:about')],[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        if data.startswith('mod:'):
            await self.handle_moderation_callback(uid,chat_id,message_id,data)

    async def handle_permission_callback(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        if uid != OWNER_ID: return
        _,action,target,key=data.split(':',3)
        target=int(target)
        async with self.db._pool().acquire() as c:
            row=await c.fetchrow('SELECT permissions FROM admins WHERE user_id=$1',target)
            if not row: return await self.safe_send(chat_id,'ادمین پیدا نشد.')
            perms=row['permissions'] or DEFAULT_PERMS.copy()
            perms[key]=not bool(perms.get(key,False))
            await c.execute('UPDATE admins SET permissions=$2 WHERE user_id=$1',target,perms)
        await self.db.log(uid,'admin_permission_changed','user',target,{'permission':key,'enabled':perms[key]})
        # Refresh the first permissions screen.
        await self.handle_admin_callback(uid,chat_id,message_id,f'own:admin:{target}')

    async def handle_target_select(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        if uid != OWNER_ID and not await self.db.is_admin(uid):
            return await self.safe_send(chat_id, '⛔ دسترسی مدیریت نداری.')
        _, _, target_type, target_id = data.split(':', 3)
        target_id = int(target_id)
        if not await self.has_perm(uid, 'search'):
            return await self.safe_send(chat_id, '⛔ مجوز جست‌وجو نداری.')
        rows = await self.db.search_target(str(target_id), target_type)
        target = rows[0] if rows else {'id': target_id, 'name': str(target_id), 'username': None}
        can_ban = await self.has_perm(uid, 'ban_user' if target_type == 'user' else 'ban_group')
        text = f"*جزئیات {'کاربر' if target_type == 'user' else 'گروه'}*\nنام: {target.get('name') or 'نامشخص'}\nشناسه: `{target_id}`\nآیدی: @{target.get('username') or 'ندارد'}"
        await self.safe_edit(chat_id, message_id, text, user_actions(target_type, target_id, can_ban))

    async def handle_moderation_callback(self, uid: int, chat_id: int, message_id: int, data: str) -> None:
        _, action, target_type, target_id = data.split(':',3)
        target_id=int(target_id)
        perm = ('ban_' if action=='ban' else 'unban_')+target_type
        if not await self.has_perm(uid,perm):
            await self.safe_send(chat_id,'⛔ این دسترسی برای حساب تو فعال نیست.')
            return
        if action == 'unban':
            case=await self.db.lift_case(target_type,target_id,uid)
            if not case:
                await self.safe_edit(chat_id,message_id,'برای این مورد محدودیت فعالی ثبت نشده است.',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
            await self.safe_edit(chat_id,message_id,f"✅ محدودیت برداشته شد.\nکد پرونده: `#{case['case_code']}`",inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return
        self.states[uid]={'step':'ban_reason','target_type':target_type,'target_id':target_id,'chat_id':chat_id,'message_id':message_id}
        await self.safe_edit(chat_id,message_id,'دلیل محدودیت را بفرست.',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]]))

    async def handle_state(self, chat_id: int, uid: int, user: dict, text: str, state: dict, msg: dict) -> bool:
        step=state.get('step')
        if step == 'search':
            results=await self.db.search_target(text,state['target_type'])
            if not results:
                await self.safe_send(chat_id,'موردی پیدا نشد. فقط کاربرها و گروه‌هایی قابل جست‌وجو هستند که ربات قبلاً آن‌ها را ثبت کرده باشد.',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]]))
                self.states.pop(uid,None); return True
            target_type=state['target_type']
            rows=[]
            for r in results[:3]: rows.append([(f"{r.get('name') or 'بدون نام'} · {r['id']}",f"adm:target:{target_type}:{r['id']}")])
            rows.append([('↩️ برگشت','admin:page:0'),('✖️ بستن','close')])
            await self.safe_send(chat_id,'*نتایج جست‌وجو*\nمورد رو انتخاب کن:',inline(rows))
            self.states.pop(uid,None); return True
        if step == 'case_lookup':
            match=re.search(r'(\d{4})',text)
            self.states.pop(uid,None)
            if not match:
                await self.safe_send(chat_id,'کد باید ۴ رقم داشته باشد؛ مثال `#5453`'); return True
            case=await self.db.find_case(int(match.group(1)))
            if not case:
                await self.safe_send(chat_id,'این کد پیگیری در دیتابیس پیدا نشد.'); return True
            await self.safe_send(chat_id,self.case_text(case),inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return True
        if step == 'edit_about':
            if not text: await self.safe_send(chat_id,'متن خالی قابل ذخیره نیست.'); return True
            await self.db.set_setting('about',text,uid); await self.db.log(uid,'about_updated',details={'length':len(text)})
            self.states.pop(uid,None); await self.safe_send(chat_id,'✅ متن «درباره ما» ذخیره شد.',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return True
        if step == 'admin_add_id':
            if not text.isdigit(): await self.safe_send(chat_id,'شناسه باید عددی باشد؛ دوباره بفرست.'); return True
            target=int(text)
            if target==OWNER_ID: await self.safe_send(chat_id,'مالک را نمی‌توان به‌عنوان ادمین معمولی اضافه کرد.'); return True
            async with self.db._pool().acquire() as c:
                await c.execute('INSERT INTO admins(user_id,display_name,permissions,added_by) VALUES($1,$2,$3,$4) ON CONFLICT(user_id) DO UPDATE SET active=TRUE,added_by=EXCLUDED.added_by',target,'',DEFAULT_PERMS,uid)
            await self.db.log(uid,'admin_added','user',target)
            self.states.pop(uid,None); await self.safe_send(chat_id,f'✅ ادمین `{target}` اضافه شد.\nمجوز بن کاربر و گروه به‌صورت پیش‌فرض خاموش است.',inline([[('⚙️ تنظیم دسترسی‌ها',f'own:admin:{target}')],[('↩️ برگشت','own:admins'),('✖️ بستن','close')]])); return True
        if step == 'team_edit_name':
            state['team_data']['name']=text; state['step']='team_edit_title'
            await self.safe_send(chat_id,'عنوان جدید را بفرست.'); return True
        if step == 'team_edit_title':
            state['team_data']['title']=text; state['step']='team_edit_description'
            await self.safe_send(chat_id,'توضیح جدید را بفرست؛ برای خالی‌گذاشتن یک خط تیره بفرست.'); return True
        if step == 'team_edit_description':
            state['team_data']['description']='' if text=='-' else text; state['step']='team_edit_username'
            await self.safe_send(chat_id,'آیدی عمومی جدید را بفرست؛ اگر ندارد یک خط تیره بفرست.'); return True
        if step == 'team_edit_username':
            d=state['team_data']; username='' if text=='-' else text.lstrip('@'); member_id=int(state['member_id'])
            async with self.db._pool().acquire() as c:
                await c.execute('UPDATE team_members SET name=$2,title=$3,description=$4,username=$5 WHERE id=$1',member_id,d['name'],d['title'],d['description'],username)
            await self.db.log(uid,'team_member_updated','team_member',member_id)
            self.states.pop(uid,None); await self.safe_send(chat_id,'✅ اطلاعات عضو تیم ویرایش شد.',inline([[('↩️ برگشت','own:team'),('✖️ بستن','close')]])); return True
        if step == 'team_add_name':
            state['team_data']['name']=text; state['step']='team_add_title'
            await self.safe_send(chat_id,'عنوان یا نقش این عضو را بفرست.'); return True
        if step == 'team_add_title':
            state['team_data']['title']=text; state['step']='team_add_description'
            await self.safe_send(chat_id,'توضیح کوتاه عضو را بفرست؛ اگر توضیح ندارد یک خط تیره بفرست.'); return True
        if step == 'team_add_description':
            state['team_data']['description']='' if text=='-' else text; state['step']='team_add_username'
            await self.safe_send(chat_id,'آیدی عمومی عضو را بفرست؛ اگر ندارد یک خط تیره بفرست.'); return True
        if step == 'team_add_username':
            d=state['team_data']; username='' if text=='-' else text.lstrip('@')
            await self.db.add_team_member(d['name'],d['title'],d['description'],username,uid)
            self.states.pop(uid,None); await self.safe_send(chat_id,'✅ عضو تیم اضافه شد.',inline([[('↩️ برگشت','own:team'),('✖️ بستن','close')]])); return True
        if step == 'ban_reason':
            state['reason']=text or 'دلیلی ثبت نشده'; state['step']='ban_duration'
            await self.safe_send(chat_id,'مدت محدودیت رو به روز بفرست؛ برای دائمی بنویس `دائمی` و برای یک روز `1`.',inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]])); return True
        if step == 'ban_duration':
            permanent=text.strip() in ('دائمی','دائم','بی پایان','بی‌پایان','permanent','forever')
            days=None
            if not permanent:
                match=re.search(r'\d+',text)
                if not match or int(match.group())<1 or int(match.group())>36500:
                    await self.safe_send(chat_id,'مدت باید تعداد روز مثبت یا «دائمی» باشد.'); return True
                days=int(match.group())
            target_type=state['target_type']; target_id=int(state['target_id'])
            info=await self.db.search_target(str(target_id),target_type)
            target=info[0] if info else {'id':target_id,'username':None,'name':str(target_id)}
            case=await self.db.create_case(target_type,target_id,target.get('username'),target.get('name') or str(target_id),state.get('reason',''),days,permanent,uid)
            self.states.pop(uid,None)
            duration='دائمی' if permanent else f'{days} روز'
            notice=f"*⛔ محدودیت MEGA BOT*\nدلیل: {case['reason']}\nمدت: {duration}\nکد پیگیری: `#{case['case_code']}`\nصادرکننده: `{uid}`"
            # For group restriction, post and try to pin the notice. For user, DM is best-effort.
            sent=None
            if target_type=='group':
                sent=await self.safe_send(target_id,notice)
                if sent:
                    try: await self.api.pin_message(target_id,int(sent['message_id']))
                    except Exception as exc: log.info('Could not pin group restriction notice: %s',exc)
                # Best-effort owner notification; if the owner has never started the bot, Bale may reject the DM.
                try:
                    members=await self.api.call('getChatAdministrators', {'chat_id':target_id})
                    creator=next((m.get('user') for m in (members or []) if m.get('status')=='creator' and m.get('user')),None)
                    if creator:
                        await self.safe_send(int(creator['id']), notice+'\n\nاین گروه در MEGA BOT محدود شده است.')
                except Exception as exc:
                    log.info('Could not notify group owner: %s',exc)
            else:
                sent=await self.safe_send(target_id,notice+'\n\nبرای پیگیری، بخش *تیم من* را باز کن.')
            await self.safe_send(chat_id,f"✅ پرونده ثبت شد.\nکد: `#{case['case_code']}`\nمدت: {duration}\nاعلان: {'ارسال شد' if sent else 'ارسال پیام ممکن نشد'}",inline([[('↩️ برگشت','admin:page:0'),('✖️ بستن','close')]]))
            return True
        return False

    def case_text(self, case: dict) -> str:
        target='کاربر' if case['target_type']=='user' else 'گروه'
        duration='دائمی' if case['permanent'] else (f"{case['duration_days']} روز" if case['duration_days'] else 'نامشخص')
        active='فعال' if case['active'] else 'پایان‌یافته / رفع‌شده'
        return (f"*پرونده #{case['case_code']}*\nنوع: {target}\nشناسه: `{case['target_id']}`\nنام: {case['target_name']}\n"
                f"دلیل: {case['reason']}\nمدت: {duration}\nوضعیت: {active}\nصادرکننده: `{case['issued_by']}`\n"
                f"تاریخ ثبت: {case['created_at'].strftime('%Y-%m-%d %H:%M UTC')}")

    async def my_cases(self, chat_id: int, uid: int) -> None:
        async with self.db._pool().acquire() as c:
            rows=await c.fetch("SELECT * FROM moderation_cases WHERE target_type='user' AND target_id=$1 ORDER BY created_at DESC LIMIT 10",uid)
        if not rows:
            await self.safe_send(chat_id,'*تیم من / پیگیری*\nپرونده‌ای برای حساب تو ثبت نشده است.',USER_KEYBOARD); return
        text='*تیم من / پیگیری پرونده‌ها*\n\n'+'\n\n'.join(f"*#{r['case_code']}* — {'فعال' if r['active'] else 'پایان‌یافته'}\nدلیل: {r['reason']}\nمدت: {'دائمی' if r['permanent'] else str(r['duration_days'])+' روز'}" for r in rows)
        await self.safe_send(chat_id,text,inline([[('↩️ برگشت','home'),('✖️ بستن','close')]]))

    async def start_ttt(self, chat_id: int, creator: int, name: str) -> None:
        gid=secrets.token_hex(3)
        game={'kind':'ttt','creator':creator,'chat_id':chat_id,'board':[' ']*9,'players':[creator,None],'turn':0}
        self.game_cache[gid]=game
        await self.safe_send(chat_id,f"*بازی دوز*\nسازنده: {name}\nنفر دوم با زدن «پیوستن» وارد بازی شود.",self.ttt_keyboard(gid,game))

    def ttt_keyboard(self, gid: str, game: dict) -> dict:
        board=game['board']; rows=[]
        for r in range(3):
            rows.append([(board[r*3+c] if board[r*3+c].strip() else '▫️',f'ttt:move:{gid}:{r*3+c}') for c in range(3)])
        rows.append([('🙋 پیوستن','ttt:join:'+gid),('✖️ لغو','game:cancel:'+gid)])
        return inline(rows)

    async def handle_ttt_callback(self, query: dict, data: str) -> None:
        msg=query.get('message') or {}; chat_id=int((msg.get('chat') or {}).get('id',0)); mid=int(msg.get('message_id',0)); uid=int((query.get('from') or {}).get('id',0))
        parts=data.split(':'); action=parts[1]; gid=parts[2]; game=self.game_cache.get(gid)
        if not game: return await self.safe_send(chat_id,'این بازی تمام شده است.')
        if action=='join':
            if uid==game['creator']: return await self.safe_send(chat_id,'برای بازی، یک نفر دیگر باید بپیوندد.')
            game['players'][1]=uid
            await self.safe_edit(chat_id,mid,'*بازی دوز شروع شد!* نفرات به ترتیب بازی می‌کنند.',self.ttt_keyboard(gid,game)); return
        if action=='move':
            if not game['players'][1]: return await self.safe_send(chat_id,'اول نفر دوم باید بپیوندد.')
            idx=int(parts[3]); player_idx=game['turn']
            if uid != game['players'][player_idx]: return await self.safe_send(chat_id,'الان نوبت تو نیست.')
            if game['board'][idx].strip(): return await self.safe_send(chat_id,'این خانه پر است.')
            mark='❌' if player_idx==0 else '⭕'; game['board'][idx]=mark; game['turn']=1-player_idx
            win=self.ttt_winner(game['board'])
            if win or all(x.strip() for x in game['board']):
                outcome='مساوی شد!' if not win else f"برنده: `{game['players'][0] if win=='❌' else game['players'][1]}`"
                await self.safe_edit(chat_id,mid,'*پایان بازی دوز*\n'+outcome,self.ttt_keyboard(gid,game)); self.game_cache.pop(gid,None)
            else: await self.safe_edit(chat_id,mid,'*بازی دوز*\nنوبت بازیکن بعدی است.',self.ttt_keyboard(gid,game))

    @staticmethod
    def ttt_winner(b: list[str]) -> str | None:
        lines=[(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
        for a,c,d in lines:
            if b[a].strip() and b[a]==b[c]==b[d]: return b[a]
        return None

    async def start_one_game(self, chat_id: int, uid: int, name: str) -> None:
        gid=secrets.token_hex(3)
        self.game_cache[gid]={'kind':'one','creator':uid,'chat_id':chat_id,'players':{uid:name},'started':False}
        await self.safe_send(chat_id,f"*🎲 هرکی تک بیاره*\nسازنده: {name}\nبازیکن‌ها با عضویت وارد شوند.",inline([
            [('▶️ استارت توسط شروع‌کننده','one:start:'+gid)],
            [('🙋 عضویت در هرکی تک بیاره','one:join:'+gid),('🚪 خروج','one:leave:'+gid)],
            [('🗑️ حذف بازی','game:cancel:'+gid)],
            [('↩️ برگشت','home'),('✖️ بستن','close')]
        ]))

    async def handle_one_callback(self, query: dict, data: str) -> None:
        msg=query.get('message') or {}; chat_id=int((msg.get('chat') or {}).get('id',0)); mid=int(msg.get('message_id',0)); uid=int((query.get('from') or {}).get('id',0))
        _,action,gid=data.split(':',2); game=self.game_cache.get(gid)
        if not game: return await self.safe_send(chat_id,'این بازی دیگه فعال نیست.')
        if action=='join':
            game['players'][uid]=str(uid); await self.safe_edit(chat_id,mid,self.one_text(game),self.one_keyboard(gid,game)); return
        if action=='leave':
            game['players'].pop(uid,None); await self.safe_edit(chat_id,mid,self.one_text(game),self.one_keyboard(gid,game)); return
        if action=='start':
            if uid!=game['creator']: return await self.safe_send(chat_id,'فقط شروع‌کننده می‌تواند بازی را استارت کند.')
            if len(game['players'])<2: return await self.safe_send(chat_id,'حداقل دو بازیکن لازم است.')
            game['started']=True
            rolls={p:random.randint(1,6) for p in game['players']}
            ones=[p for p,v in rolls.items() if v==1]
            if ones: result='🏆 تک آورنده: '+', '.join(f'`{p}`' for p in ones)
            else: result='هیچکس تک نیاورد...'
            result+='\n\n'+ '\n'.join(f'`{p}`: {v}' for p,v in rolls.items())
            await self.safe_edit(chat_id,mid,'*نتیجه هرکی تک بیاره*\n'+result,inline([[('🔁 دور بعد','one:next:'+gid),('🗑️ حذف بازی','game:cancel:'+gid)],[('✖️ بستن','close')]])); return
        if action=='next':
            if uid!=game['creator']: return await self.safe_send(chat_id,'فقط شروع‌کننده می‌تواند دور بعد را اجرا کند.')
            game['started']=False; await self.safe_edit(chat_id,mid,self.one_text(game),self.one_keyboard(gid,game))

    def one_text(self, game: dict) -> str:
        players=game.get('players',{})
        return '*🎲 هرکی تک بیاره*\n\nبازیکنان:\n'+'\n'.join(f"• `{pid}` — {name}" for pid,name in players.items())+'\n\nسازنده می‌تواند بازی را شروع کند.'

    def one_keyboard(self, gid: str, game: dict) -> dict:
        return inline([[('▶️ استارت توسط شروع‌کننده','one:start:'+gid)],[('🙋 عضویت','one:join:'+gid),('🚪 خروج','one:leave:'+gid)],[('🗑️ حذف بازی','game:cancel:'+gid)],[('↩️ برگشت','home'),('✖️ بستن','close')]])
