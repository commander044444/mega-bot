from __future__ import annotations

USER_KEYBOARD = {
    'keyboard': [
        [{'text': 'درباره ما'}, {'text': 'راهنما ربات'}],
        [{'text': 'منوی بیشتر'}, {'text': 'تیم من'}],
    ],
    'resize_keyboard': True,
    'is_persistent': True,
}


def inline(rows: list[list[tuple[str, str]]]) -> dict:
    return {'inline_keyboard': [[{'text': label, 'callback_data': data} for label, data in row] for row in rows]}


def menu_page(items: list[tuple[str, str]], page: int, prefix: str, back: str = 'home', page_size: int = 3) -> dict:
    """At most 6 buttons on a page: 3 content items + next/previous + close/back controls."""
    start = page * page_size
    shown = items[start:start + page_size]
    rows = [[(label, data)] for label, data in shown]
    controls: list[tuple[str, str]] = []
    if page > 0:
        controls.append(('⬅️ قبلی', f'{prefix}:page:{page-1}'))
    if start + page_size < len(items):
        controls.append(('بعدی ➡️', f'{prefix}:page:{page+1}'))
    if controls:
        rows.append(controls[:2])
    rows.append([('↩️ برگشت', f'{prefix}:back:{back}'), ('✖️ بستن', 'close')])
    return inline(rows)


def main_admin_menu(owner: bool = False) -> dict:
    items = [
        ('📊 آمار و گزارش‌ها', 'adm:stats'),
        ('👤 جست‌وجوی کاربر', 'adm:search:user'),
        ('👥 جست‌وجوی گروه', 'adm:search:group'),
        ('🎫 بررسی کد پیگیری', 'adm:case'),
        ('🧾 گزارش فعالیت‌ها', 'adm:logs'),
    ]
    if owner:
        items += [
            ('🛡️ مدیریت ادمین‌ها', 'own:admins'),
            ('📝 ویرایش درباره ما', 'own:about'),
            ('👨‍💻 تیم توسعه‌دهنده', 'own:team'),
            ('⚙️ تنظیمات مالک', 'own:settings'),
        ]
    return menu_page(items, 0, 'admin', 'home')


def user_actions(target_type: str, target_id: int, can_ban: bool) -> dict:
    rows = []
    if can_ban:
        rows.append([('⛔ محدودسازی', f'mod:ban:{target_type}:{target_id}')])
    rows.append([('♻️ رفع محدودیت', f'mod:unban:{target_type}:{target_id}')])
    rows.append([('↩️ برگشت', f'adm:search:{target_type}'), ('✖️ بستن', 'close')])
    return inline(rows)
