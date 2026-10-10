from __future__ import annotations
import asyncpg
from datetime import datetime, timezone
from typing import Any

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT NOT NULL DEFAULT '',
  first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  starts_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS users_username_idx ON users (LOWER(username));
CREATE TABLE IF NOT EXISTS groups_seen (
  chat_id BIGINT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', username TEXT,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  messages_count BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS groups_username_idx ON groups_seen (LOWER(username));
CREATE TABLE IF NOT EXISTS admins (
  user_id BIGINT PRIMARY KEY, username TEXT, display_name TEXT NOT NULL DEFAULT '',
  permissions JSONB NOT NULL DEFAULT '{"stats":true,"search":true,"case_lookup":true,"unban_user":true,"unban_group":true,"ban_user":false,"ban_group":false,"edit_about":false,"edit_team":false,"manage_admins":false,"settings":false}'::jsonb,
  active BOOLEAN NOT NULL DEFAULT TRUE, added_by BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_by BIGINT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS team_members (
  id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
  username TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS moderation_cases (
  id BIGSERIAL PRIMARY KEY, case_code INTEGER UNIQUE NOT NULL CHECK (case_code BETWEEN 1000 AND 9999),
  target_type TEXT NOT NULL CHECK (target_type IN ('user','group')), target_id BIGINT NOT NULL,
  target_username TEXT, target_name TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL,
  duration_days INTEGER, permanent BOOLEAN NOT NULL DEFAULT FALSE, active BOOLEAN NOT NULL DEFAULT TRUE,
  action TEXT NOT NULL DEFAULT 'ban', issued_by BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), expires_at TIMESTAMPTZ,
  lifted_by BIGINT, lifted_at TIMESTAMPTZ, lift_reason TEXT
);
CREATE INDEX IF NOT EXISTS cases_target_idx ON moderation_cases(target_type, target_id, active);
CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGSERIAL PRIMARY KEY, actor_id BIGINT NOT NULL, action TEXT NOT NULL,
  target_type TEXT, target_id BIGINT, details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS usage_events (
  id BIGSERIAL PRIMARY KEY, user_id BIGINT, chat_id BIGINT NOT NULL, chat_type TEXT NOT NULL,
  event_type TEXT NOT NULL DEFAULT 'message', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS usage_events_date_idx ON usage_events(created_at);
CREATE TABLE IF NOT EXISTS games (
  id TEXT PRIMARY KEY, chat_id BIGINT NOT NULL, creator_id BIGINT NOT NULL,
  kind TEXT NOT NULL, state JSONB NOT NULL DEFAULT '{}'::jsonb,
  active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        async def init_connection(conn):
            await conn.set_type_codec('json', encoder=lambda v: __import__('json').dumps(v, ensure_ascii=False), decoder=__import__('json').loads, schema='pg_catalog')
            await conn.set_type_codec('jsonb', encoder=lambda v: __import__('json').dumps(v, ensure_ascii=False), decoder=__import__('json').loads, schema='pg_catalog')
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=8, command_timeout=30, init=init_connection)
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)
            await conn.execute("INSERT INTO settings(key,value) VALUES('about', to_jsonb($1::text)) ON CONFLICT(key) DO NOTHING", "MEGA BOT یک ربات اجتماعی برای بله است.")
            await conn.execute("INSERT INTO settings(key,value) VALUES('donations_total', '0'::jsonb) ON CONFLICT(key) DO NOTHING")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError('Database is not connected')
        return self.pool

    async def upsert_user(self, user: dict[str, Any]) -> None:
        uid = user.get('id')
        if not uid:
            return
        async with self._pool().acquire() as c:
            await c.execute("""INSERT INTO users(user_id,username,first_name) VALUES($1,$2,$3)
              ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_seen=NOW()""",
              int(uid), user.get('username'), user.get('first_name') or user.get('name') or '')

    async def upsert_group(self, chat: dict[str, Any]) -> None:
        cid = chat.get('id')
        if not cid:
            return
        async with self._pool().acquire() as c:
            await c.execute("""INSERT INTO groups_seen(chat_id,title,username) VALUES($1,$2,$3)
              ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title, username=EXCLUDED.username, last_seen=NOW()""",
              int(cid), chat.get('title') or chat.get('name') or '', chat.get('username'))

    async def get_setting(self, key: str, default: Any = None) -> Any:
        async with self._pool().acquire() as c:
            row = await c.fetchrow('SELECT value FROM settings WHERE key=$1', key)
        return row['value'] if row else default

    async def set_setting(self, key: str, value: Any, actor: int) -> None:
        import json
        async with self._pool().acquire() as c:
            await c.execute("""INSERT INTO settings(key,value,updated_by,updated_at) VALUES($1,$2,$3,NOW())
              ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
              key, value, actor)

    async def log(self, actor: int, action: str, target_type: str | None = None, target_id: int | None = None, details: dict | None = None) -> None:
        import json
        async with self._pool().acquire() as c:
            await c.execute('INSERT INTO audit_logs(actor_id,action,target_type,target_id,details) VALUES($1,$2,$3,$4,$5)',
                            actor, action, target_type, target_id, details or {})

    async def is_admin(self, user_id: int) -> bool:
        async with self._pool().acquire() as c:
            return bool(await c.fetchval('SELECT active FROM admins WHERE user_id=$1', user_id))

    async def get_admin(self, user_id: int) -> dict | None:
        async with self._pool().acquire() as c:
            row = await c.fetchrow('SELECT * FROM admins WHERE user_id=$1 AND active=TRUE', user_id)
        return dict(row) if row else None

    async def list_admins(self, limit: int = 50, offset: int = 0) -> list[dict]:
        async with self._pool().acquire() as c:
            rows = await c.fetch('SELECT * FROM admins ORDER BY created_at DESC LIMIT $1 OFFSET $2', limit, offset)
        return [dict(r) for r in rows]

    async def search_target(self, term: str, target_type: str) -> list[dict]:
        async with self._pool().acquire() as c:
            if target_type == 'user':
                if term.lstrip('-').isdigit():
                    rows = await c.fetch('SELECT user_id AS id, username, first_name AS name FROM users WHERE user_id=$1 LIMIT 10', int(term))
                else:
                    name = term.lstrip('@').lower()
                    rows = await c.fetch('SELECT user_id AS id, username, first_name AS name FROM users WHERE LOWER(username) LIKE $1 ORDER BY last_seen DESC LIMIT 10', f'%{name}%')
            else:
                if term.lstrip('-').isdigit():
                    rows = await c.fetch('SELECT chat_id AS id, username, title AS name FROM groups_seen WHERE chat_id=$1 LIMIT 10', int(term))
                else:
                    name = term.lstrip('@').lower()
                    rows = await c.fetch('SELECT chat_id AS id, username, title AS name FROM groups_seen WHERE LOWER(username) LIKE $1 ORDER BY last_seen DESC LIMIT 10', f'%{name}%')
        return [dict(r) for r in rows]

    async def stats(self) -> dict:
        async with self._pool().acquire() as c:
            return {
                'users': await c.fetchval('SELECT COUNT(*) FROM users'),
                'new_today': await c.fetchval("SELECT COUNT(*) FROM users WHERE first_seen >= date_trunc('day', NOW())"),
                'groups': await c.fetchval('SELECT COUNT(*) FROM groups_seen'),
                'group_users': await c.fetchval("SELECT COUNT(DISTINCT user_id) FROM usage_events WHERE chat_type IN ('group','supergroup') AND user_id IS NOT NULL"),
                'banned_users': await c.fetchval("SELECT COUNT(*) FROM moderation_cases WHERE target_type='user' AND active=TRUE AND (expires_at IS NULL OR expires_at>NOW())"),
                'banned_groups': await c.fetchval("SELECT COUNT(*) FROM moderation_cases WHERE target_type='group' AND active=TRUE AND (expires_at IS NULL OR expires_at>NOW())"),
                'donations': await c.fetchval("SELECT COALESCE((value #>> '{}')::bigint,0) FROM settings WHERE key='donations_total'") or 0,
            }

    async def record_usage(self, user_id: int | None, chat: dict, event_type: str = 'message') -> None:
        chat_type = chat.get('type') or 'private'
        async with self._pool().acquire() as c:
            await c.execute('INSERT INTO usage_events(user_id,chat_id,chat_type,event_type) VALUES($1,$2,$3,$4)', user_id, int(chat['id']), chat_type, event_type)
            if chat_type in ('group','supergroup'):
                await c.execute('UPDATE groups_seen SET messages_count=messages_count+1,last_seen=NOW() WHERE chat_id=$1', int(chat['id']))

    async def create_case(self, target_type: str, target_id: int, username: str | None, name: str, reason: str, days: int | None, permanent: bool, actor: int) -> dict:
        from datetime import timedelta
        async with self._pool().acquire() as c:
            async with c.transaction():
                await c.execute('SELECT pg_advisory_xact_lock(7402916)')
                used = await c.fetch('SELECT case_code FROM moderation_cases')
                occupied = {int(r['case_code']) for r in used}
                code = next((x for x in range(1000, 10000) if x not in occupied), None)
                if code is None:
                    raise RuntimeError('ظرفیت کدهای پیگیری چهارتایی تمام شده است.')
                expires = None if permanent or not days else datetime.now(timezone.utc) + timedelta(days=days)
                row = await c.fetchrow("""INSERT INTO moderation_cases(case_code,target_type,target_id,target_username,target_name,reason,duration_days,permanent,issued_by,expires_at)
                  VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *""", code,target_type,target_id,username,name,reason,days,permanent,actor,expires)
                await c.execute('INSERT INTO audit_logs(actor_id,action,target_type,target_id,details) VALUES($1,$2,$3,$4,$5)', actor, 'ban_'+target_type, target_type, target_id, {'case_code': code})
        return dict(row)

    async def lift_case(self, target_type: str, target_id: int, actor: int, reason: str = 'رفع محدودیت توسط مدیریت') -> dict | None:
        async with self._pool().acquire() as c:
            row = await c.fetchrow("""UPDATE moderation_cases SET active=FALSE,action='unban',lifted_by=$3,lifted_at=NOW(),lift_reason=$4
              WHERE id=(SELECT id FROM moderation_cases WHERE target_type=$1 AND target_id=$2 AND active=TRUE ORDER BY created_at DESC LIMIT 1) RETURNING *""", target_type,target_id,actor,reason)
            if row:
                await c.execute('INSERT INTO audit_logs(actor_id,action,target_type,target_id,details) VALUES($1,$2,$3,$4,$5)', actor, 'unban_'+target_type, target_type, target_id, {'case_code': row['case_code']})
        return dict(row) if row else None

    async def find_case(self, code: int) -> dict | None:
        async with self._pool().acquire() as c:
            row = await c.fetchrow('SELECT * FROM moderation_cases WHERE case_code=$1', code)
        return dict(row) if row else None

    async def is_restricted(self, target_type: str, target_id: int) -> dict | None:
        async with self._pool().acquire() as c:
            row = await c.fetchrow("SELECT * FROM moderation_cases WHERE target_type=$1 AND target_id=$2 AND active=TRUE ORDER BY created_at DESC LIMIT 1", target_type,target_id)
            if row and row['expires_at'] and row['expires_at'] <= datetime.now(timezone.utc):
                await c.execute('UPDATE moderation_cases SET active=FALSE,action=\'expired\' WHERE id=$1', row['id'])
                return None
        return dict(row) if row else None

    async def add_team_member(self, name: str, title: str, description: str, username: str, actor: int) -> None:
        async with self._pool().acquire() as c:
            await c.execute('INSERT INTO team_members(name,title,description,username,sort_order) VALUES($1,$2,$3,$4,COALESCE((SELECT MAX(sort_order)+1 FROM team_members),0))', name,title,description,username)
        await self.log(actor, 'team_member_added', details={'name':name})

    async def get_team(self) -> list[dict]:
        async with self._pool().acquire() as c:
            rows = await c.fetch('SELECT * FROM team_members ORDER BY sort_order,id')
        return [dict(r) for r in rows]

    async def delete_team_member(self, member_id: int, actor: int) -> bool:
        async with self._pool().acquire() as c:
            result = await c.execute('DELETE FROM team_members WHERE id=$1', member_id)
        await self.log(actor, 'team_member_deleted', target_type='team_member', target_id=member_id)
        return result.endswith('1')
