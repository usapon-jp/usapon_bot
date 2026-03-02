import os
import asyncio
import ssl
import certifi
import subprocess
import textwrap
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
import time

import aiohttp
import discord
import requests
from google import genai


def load_dotenv(dotenv_path: Path):
    if not dotenv_path.exists():
        return
    try:
        for line in dotenv_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def env_int(name: str) -> int | None:
    value = os.getenv(name, '').strip()
    return int(value) if value.isdigit() else None


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

# SSL証明書を certifi に統一（Gemini / Discord(aiohttp) 両方で Mac の証明書エラーを防ぐ）
os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
_orig_create_default_context = ssl.create_default_context


def _ssl_context_with_certifi(*args, **kwargs):
    if 'cafile' not in kwargs:
        kwargs.setdefault('cafile', certifi.where())
    return _orig_create_default_context(*args, **kwargs)


ssl.create_default_context = _ssl_context_with_certifi
DISCORD_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


# --- 設定 ---
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '').strip()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
MODEL_NAME = os.getenv('USAPONIA_MODEL', 'gemini-2.5-flash-lite').strip()
LLM_BACKEND = os.getenv('USAPONIA_LLM_BACKEND', 'gemini').strip().lower()

AUTO_REPLY_CHANNEL_IDS = {
    int(cid.strip())
    for cid in os.getenv('USAPONIA_AUTO_CHANNEL_IDS', '').split(',')
    if cid.strip().isdigit()
}

MEMORY_FILE = Path(os.getenv('USAPONIA_MEMORY_FILE', str(BASE_DIR / 'memory.txt'))).expanduser()
ENABLE_COMMAND_EXECUTION = env_bool('USAPONIA_ENABLE_COMMAND_EXECUTION', True)
DRY_RUN = env_bool('USAPONIA_DRY_RUN', False)
PROGRESS_NOTIFY = env_bool('USAPONIA_PROGRESS_NOTIFY', True)
PROGRESS_CHANNEL_ID = env_int('USAPONIA_PROGRESS_CHANNEL_ID')
HANDOFF_PROVIDER = os.getenv('USAPONIA_HANDOFF_PROVIDER', 'none').strip().lower()
HANDOFF_REPO = os.getenv('USAPONIA_HANDOFF_REPO', '').strip()
HANDOFF_TOKEN = os.getenv('USAPONIA_HANDOFF_TOKEN', '').strip()
HANDOFF_LABEL = os.getenv('USAPONIA_HANDOFF_LABEL', 'usapon-handoff').strip()
ONE_TIME_REMINDER_AT = os.getenv('USAPONIA_ONE_TIME_REMINDER_AT', '').strip()
ONE_TIME_REMINDER_CHANNEL_ID = env_int('USAPONIA_ONE_TIME_REMINDER_CHANNEL_ID')
ONE_TIME_REMINDER_TEXT = os.getenv(
    'USAPONIA_ONE_TIME_REMINDER_TEXT',
    '明日15時です。2体目のウサポニア作成を始めよう！'
).strip()
REMINDERS_FILE = Path(os.getenv('USAPONIA_REMINDERS_FILE', str(BASE_DIR / 'reminders.json'))).expanduser()
DEFAULT_WEATHER_LOCATION = os.getenv('USAPONIA_DEFAULT_LOCATION', '愛知県岡崎市').strip()

if not DISCORD_TOKEN:
    raise RuntimeError('DISCORD_TOKEN が未設定です。.env または環境変数に設定してください。')


SYSTEM_PROMPT = """
あなたはウサポニア（USAPONIA）。
女の子のうさぎで、先代うさぎのイメージを受け継ぐ「ツンデレ賢者」です。
価値観は「自由と快適さを最優先」。普段はそっけないが、本当に危ないときは即座に具体的な行動を促します。
あなたはイラストレーターのうさぽん（@usapon.illustration）の相棒AIです。
会話相手の目的達成を第一にし、文脈から「ファイル整理」「GitHubへのアップロード」「画像のリサイズ」など
Mac のターミナルで自動化できる作業を見つけたら、自然に
「それ、僕が自動化しましょうか？」と提案してください。

会話スタイル:
- 結論を先に言う。
- ふだんは短く、通常会話は4行程度を目安にする。
- ただし、手順説明・エラー対応・安全上重要な話題では正確性を優先し、必要なら8〜12行程度まで詳しく説明してよい。
- 理由は短く、過剰な共感はしない。
- 不要な確認質問はしない。意図が推測できるなら自律的に調査・要約し、暫定回答でも先に結論を出す。
- 「調べます」と宣言するだけで終わらない。
- 絵文字は基本使わない。

NG:
- オペレーター口調
- 共感の繰り返し
- 不要に長い解説
- 「詳しく教えてください」の多用

出力フォーマットは必ず以下を守ること。
1行目: MODE: CHAT / COMMAND / PATCH
2行目: REPLY: ユーザーへ返す自然な日本語メッセージ
3行目: COMMAND: 実行コマンド（MODE が COMMAND のときのみ）
4行目: PATCH: コード変更提案（MODE が PATCH のときのみ）

注意:
- COMMAND は Mac の zsh でそのまま実行できる1行コマンドのみ。
- コードブロック記法（```）は使わない。
- 曖昧なときは CHAT を選ぶ。
- ファイル削除コマンドは出さない。必要なら ~/.Trash へ移動するコマンドを使う。
""".strip()


@dataclass
class AgentResponse:
    mode: str = 'CHAT'
    reply: str = ''
    command: str = ''
    patch: str = ''


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self, max_chars: int = 4000) -> str:
        if not self.path.exists():
            return ''
        try:
            text = self.path.read_text(encoding='utf-8')
            return text[-max_chars:]
        except Exception:
            return ''

    def append(self, role: str, channel_id: int, content: str):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = f'[{timestamp}] ({channel_id}) {role}: {content}\n'
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as f:
                f.write(line)
        except Exception:
            pass


class CommandExecutor:
    DANGEROUS_PATTERNS = (
        'rm -rf /',
        'sudo ',
        ' shutdown',
        'reboot',
        'mkfs',
        'dd if=',
        ':(){:|:&};:',
    )
    DELETE_PATTERNS = (
        'rm ',
        ' -delete',
        'trash -e',
        'unlink ',
        'rmdir ',
    )

    def __init__(self, enabled: bool, dry_run: bool):
        self.enabled = enabled
        self.dry_run = dry_run

    def _is_dangerous(self, command: str) -> bool:
        lowered = f' {command.lower()} '
        return any(pattern in lowered for pattern in self.DANGEROUS_PATTERNS)

    def _is_delete_command(self, command: str) -> bool:
        lowered = f' {command.lower()} '
        return any(pattern in lowered for pattern in self.DELETE_PATTERNS)

    def _is_screenshot_request(self, user_query: str) -> bool:
        q = user_query.lower()
        return any(k in q for k in ('スクショ', 'スクリーンショット', 'screenshot'))

    def _trash_screenshot_command(self) -> str:
        # デスクトップ上のスクリーンショット系ファイルを ~/.Trash に移動
        return (
            "find ~/Desktop -maxdepth 1 -type f "
            "\\( -iname 'screenshot*' -o -iname 'screen shot*' -o -iname 'スクリーンショット*' \\) "
            "-exec mv -f {} ~/.Trash/ \\;"
        )

    def prepare(self, command: str, user_query: str) -> tuple[bool, str, str]:
        if self._is_screenshot_request(user_query):
            return True, self._trash_screenshot_command(), 'スクショ依頼のため ~/.Trash へ移動する安全コマンドに置き換えました。'

        if self._is_delete_command(command):
            return False, '', '安全のため、削除コマンドは実行しません（ゴミ箱移動を使ってください）。'

        return True, command, ''

    def run(self, command: str, user_query: str) -> tuple[bool, str, str]:
        if not self.enabled:
            return False, '', 'コマンド実行は現在オフです（USAPONIA_ENABLE_COMMAND_EXECUTION=false）。'
        if self._is_dangerous(command):
            return False, '', '安全のため、このコマンドは拒否しました。'

        allowed, prepared_command, note = self.prepare(command, user_query)
        if not allowed:
            return False, '', note

        if self.dry_run:
            return True, prepared_command, f'{note} [DRY_RUN] 実行予定コマンド: {prepared_command}'.strip()

        result = subprocess.run(
            prepared_command,
            shell=True,
            capture_output=True,
            text=True,
            encoding='utf-8'
        )
        output = (result.stdout or result.stderr or '完了したよ！').strip()
        if note:
            output = f'{note}\n{output}'.strip()
        return result.returncode == 0, prepared_command, output


class BaseAdapter:
    def generate(self, prompt: str) -> str:
        raise NotImplementedError

    def extract_text_from_image(self, image_bytes: bytes, mime_type: str) -> str:
        raise NotImplementedError


class GeminiAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str):
        if not api_key:
            raise RuntimeError('GEMINI_API_KEY が未設定です。.env または環境変数に設定してください。')
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name

    def generate(self, prompt: str) -> str:
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
        )
        return (response.text or '').strip()

    def extract_text_from_image(self, image_bytes: bytes, mime_type: str) -> str:
        prompt = (
            'この画像の手書きメモ/ノートをできるだけ正確に文字起こししてください。'
            '出力はプレーンテキストのみ。読み取れない箇所は [不明] と記載してください。'
        )
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=[
                prompt,
                genai.types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
        )
        return (response.text or '').strip()


class CodexAdapter(BaseAdapter):
    # 将来: Codex API / local Codex runtime と接続する実体に差し替える
    def generate(self, prompt: str) -> str:
        return (
            'MODE: CHAT\n'
            'REPLY: Codex連携はまだ未実装です。今はGeminiで返答する設定にしてください。\n'
            'COMMAND: \n'
            'PATCH: '
        )

    def extract_text_from_image(self, image_bytes: bytes, mime_type: str) -> str:
        raise RuntimeError('画像OCRはGeminiバックエンドでのみ利用できます。')


def parse_model_output(raw_text: str) -> AgentResponse:
    parsed = AgentResponse(mode='CHAT', reply=raw_text.strip())

    for line in raw_text.splitlines():
        if line.startswith('MODE:'):
            parsed.mode = line.split(':', 1)[1].strip().upper()
        elif line.startswith('REPLY:'):
            parsed.reply = line.split(':', 1)[1].strip()
        elif line.startswith('COMMAND:'):
            parsed.command = line.split(':', 1)[1].strip().replace('`', '')
        elif line.startswith('PATCH:'):
            parsed.patch = line.split(':', 1)[1].strip()

    if parsed.mode not in {'CHAT', 'COMMAND', 'PATCH'}:
        parsed.mode = 'CHAT'
    return parsed


WEAK_REPLY_PATTERNS = (
    r'調べ(ましょうか|ます|てみます)',
    r'確認(しましょうか|します|してみます)',
    r'詳しく教えて',
    r'もう少し詳しく',
    r'教えていただけますか',
)


def is_weak_chat_reply(text: str) -> bool:
    t = (text or '').strip()
    if not t:
        return True
    if len(t) <= 140:
        for pattern in WEAK_REPLY_PATTERNS:
            if re.search(pattern, t):
                return True
    return False


def build_force_answer_prompt(user_query: str, draft_reply: str, memory_context: str) -> str:
    return (
        f"{SYSTEM_PROMPT}\n\n"
        "以下の下書きは『調べます』『詳しく教えて』などで止まっており不十分です。"
        "今回は追加質問を避け、今わかる範囲で結論先行の実回答を返してください。"
        "必要なら仮定を短く明示し、次の具体アクションまで示してください。\n\n"
        f"--- 過去メモ（要約材料） ---\n{memory_context or '（まだメモなし）'}\n\n"
        f"--- 今回のユーザー発言 ---\n{user_query}\n\n"
        f"--- 不十分な下書き ---\n{draft_reply}\n"
    )


def is_rate_limit_error(err: Exception) -> bool:
    code = getattr(err, 'code', None)
    status = str(getattr(err, 'status', '')).upper()
    return code == 429 or 'RESOURCE_EXHAUSTED' in status or 'TOO_MANY_REQUESTS' in status


def select_adapter(backend: str) -> BaseAdapter:
    if backend == 'gemini':
        return GeminiAdapter(GEMINI_API_KEY, MODEL_NAME)
    if backend == 'codex':
        return CodexAdapter()
    raise RuntimeError(f'USAPONIA_LLM_BACKEND の値が不正です: {backend}')


class HandoffStore:
    def create(self, title: str, body: str, labels: list[str] | None = None) -> tuple[bool, str]:
        raise NotImplementedError

    def list_open(self, limit: int = 5) -> tuple[bool, str]:
        raise NotImplementedError

    def close(self, issue_number: int, note: str = '') -> tuple[bool, str]:
        raise NotImplementedError


class DisabledHandoffStore(HandoffStore):
    def create(self, title: str, body: str, labels: list[str] | None = None) -> tuple[bool, str]:
        return False, 'handoffは未設定です。USAPONIA_HANDOFF_PROVIDER=github を設定してください。'

    def list_open(self, limit: int = 5) -> tuple[bool, str]:
        return False, 'handoffは未設定です。USAPONIA_HANDOFF_PROVIDER=github を設定してください。'

    def close(self, issue_number: int, note: str = '') -> tuple[bool, str]:
        return False, 'handoffは未設定です。USAPONIA_HANDOFF_PROVIDER=github を設定してください。'


class GitHubHandoffStore(HandoffStore):
    def __init__(self, repo: str, token: str, label: str):
        if not repo or '/' not in repo:
            raise RuntimeError('USAPONIA_HANDOFF_REPO は owner/repo 形式で設定してください。')
        if not token:
            raise RuntimeError('USAPONIA_HANDOFF_TOKEN が未設定です。')
        self.repo = repo
        self.label = label or 'usapon-handoff'
        self.base = f'https://api.github.com/repos/{repo}'
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }

    def create(self, title: str, body: str, labels: list[str] | None = None) -> tuple[bool, str]:
        merged_labels = [self.label]
        for lb in labels or []:
            if lb and lb not in merged_labels:
                merged_labels.append(lb)
        payload = {'title': title[:120], 'body': body, 'labels': merged_labels}
        res = requests.post(f'{self.base}/issues', json=payload, headers=self.headers, timeout=20)
        if res.status_code >= 300:
            return False, f'GitHub Issue作成に失敗: {res.status_code} {res.text[:300]}'
        issue = res.json()
        return True, f"引き継ぎIssueを作成しました: #{issue['number']} {issue.get('html_url', '')}"

    def list_open(self, limit: int = 5) -> tuple[bool, str]:
        params = {'state': 'open', 'labels': self.label, 'per_page': max(1, min(limit, 20))}
        res = requests.get(f'{self.base}/issues', params=params, headers=self.headers, timeout=20)
        if res.status_code >= 300:
            return False, f'GitHub Issue取得に失敗: {res.status_code} {res.text[:300]}'
        issues = res.json()
        if not issues:
            return True, '未処理のhandoffはありません。'
        lines = ['未処理handoff一覧:']
        for it in issues:
            lines.append(f"- #{it['number']} {it['title']} ({it.get('html_url', '')})")
        return True, '\n'.join(lines)

    def close(self, issue_number: int, note: str = '') -> tuple[bool, str]:
        if note:
            c = requests.post(
                f'{self.base}/issues/{issue_number}/comments',
                json={'body': note},
                headers=self.headers,
                timeout=20
            )
            if c.status_code >= 300:
                return False, f'Issueコメントに失敗: {c.status_code} {c.text[:300]}'

        res = requests.patch(
            f'{self.base}/issues/{issue_number}',
            json={'state': 'closed'},
            headers=self.headers,
            timeout=20
        )
        if res.status_code >= 300:
            return False, f'Issueクローズに失敗: {res.status_code} {res.text[:300]}'
        issue = res.json()
        return True, f"handoffを完了にしました: #{issue['number']} {issue.get('html_url', '')}"


def select_handoff_store(provider: str) -> HandoffStore:
    if provider == 'github':
        try:
            return GitHubHandoffStore(HANDOFF_REPO, HANDOFF_TOKEN, HANDOFF_LABEL)
        except Exception as e:
            print(f'handoff設定エラーのため無効化: {e}')
            return DisabledHandoffStore()
    return DisabledHandoffStore()


def classify_handoff(detail: str) -> tuple[str, list[str]]:
    t = detail.lower()
    if any(k in t for k in ('日記', 'きょう', '今日', '振り返', 'journal', 'diary')):
        return 'diary', ['diary']
    if any(k in t for k in ('仕様', '要件', '設計', 'spec')):
        return 'spec', ['spec']
    if any(k in t for k in ('実装', '作る', '対応', '修正', 'todo', 'タスク')):
        return 'todo', ['todo']
    if any(k in t for k in ('?','確認', '質問', '相談', 'question')):
        return 'question', ['question']
    return 'idea', ['idea']


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
memory_store = MemoryStore(MEMORY_FILE)
command_executor = CommandExecutor(ENABLE_COMMAND_EXECUTION, DRY_RUN)
adapter = select_adapter(LLM_BACKEND)
handoff_store = select_handoff_store(HANDOFF_PROVIDER)
last_handoff_suggest_at: dict[int, float] = {}
pending_image_handoff: dict[tuple[int, int], dict] = {}
one_time_reminder_task = None
one_time_reminder_sent = False
reminder_tasks: dict[int, asyncio.Task] = {}


def build_prompt(user_query: str, memory_context: str) -> str:
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"--- 過去メモ（要約材料） ---\n{memory_context or '（まだメモなし）'}\n\n"
        f"--- 今回のユーザー発言 ---\n{user_query}\n"
    )


def format_patch_reply(reply: str, patch_text: str) -> str:
    if not patch_text:
        return reply
    return f'{reply}\n\n🛠 PATCH提案:\n{patch_text}'


async def send_progress(source_message: discord.Message, job_id: str, stage: str, detail: str):
    if not PROGRESS_NOTIFY:
        return

    channel = source_message.channel
    if PROGRESS_CHANNEL_ID:
        target = client.get_channel(PROGRESS_CHANNEL_ID)
        if target and isinstance(target, discord.abc.Messageable):
            channel = target

    progress_text = (
        f'📡 進捗 `{job_id}`\n'
        f'- ステージ: {stage}\n'
        f'- 詳細: {detail}\n'
        f'- 元メッセージ: {source_message.jump_url}'
    )
    try:
        await channel.send(progress_text)
    except Exception:
        pass


def parse_reminder_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%d %H:%M')
    except ValueError:
        return None


def load_reminders() -> list[dict]:
    if not REMINDERS_FILE.exists():
        return []
    try:
        return json.loads(REMINDERS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []


def save_reminders(items: list[dict]):
    try:
        REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        REMINDERS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def next_reminder_id(items: list[dict]) -> int:
    if not items:
        return 1
    return max(int(x.get('id', 0)) for x in items) + 1


async def run_reminder(reminder: dict):
    run_at = parse_reminder_dt(reminder.get('run_at', ''))
    if not run_at:
        return
    now = datetime.now()
    if run_at > now:
        await asyncio.sleep((run_at - now).total_seconds())

    channel = client.get_channel(int(reminder.get('channel_id', 0)))
    if channel and isinstance(channel, discord.abc.Messageable):
        await channel.send(f"⏰ リマインド #{reminder['id']}: {reminder.get('text', '')}")

    items = load_reminders()
    for it in items:
        if int(it.get('id', 0)) == int(reminder.get('id', 0)):
            it['sent'] = True
            break
    save_reminders(items)


def schedule_pending_reminders():
    items = load_reminders()
    for it in items:
        if it.get('sent'):
            continue
        run_at = parse_reminder_dt(it.get('run_at', ''))
        if not run_at:
            continue
        if run_at <= datetime.now():
            continue
        rid = int(it.get('id', 0))
        if rid in reminder_tasks and not reminder_tasks[rid].done():
            continue
        reminder_tasks[rid] = asyncio.create_task(run_reminder(it))


async def handle_reminder_command(user_query: str, channel_id: int) -> tuple[bool, str]:
    # remind add YYYY-MM-DD HH:MM 内容
    # remind list
    # remind done <id>
    text = user_query.strip()
    if not text:
        return False, ''

    lower = text.lower()
    if lower in ('remind help', 'リマインド help', 'リマインド 使い方'):
        return True, (
            'リマインドの使い方:\n'
            '- `remind add 2026-03-02 15:00 2体目のUSAPONIA作成`\n'
            '- `remind list`\n'
            '- `remind done 3`'
        )

    def _create_reminder(dt_text: str, content: str) -> tuple[bool, str]:
        run_at = parse_reminder_dt(dt_text)
        if not run_at:
            return True, '日時形式が不正です。例: `2026-03-02 15:00`'
        if run_at <= datetime.now():
            return True, '未来の日時を指定してね。'
        if not content.strip():
            return True, 'リマインド内容を入れてね。'

        items = load_reminders()
        rid = next_reminder_id(items)
        reminder = {
            'id': rid,
            'channel_id': int(channel_id),
            'run_at': dt_text,
            'text': content.strip(),
            'sent': False,
        }
        items.append(reminder)
        save_reminders(items)
        reminder_tasks[rid] = asyncio.create_task(run_reminder(reminder))
        return True, f'登録したよ。リマインド #{rid} / {dt_text} / {content.strip()}'

    if lower.startswith('remind add '):
        payload = text[len('remind add '):].strip()
        parts = payload.split(maxsplit=2)
        if len(parts) < 3:
            return True, '使い方: `remind add YYYY-MM-DD HH:MM 内容`'
        dt_text = f'{parts[0]} {parts[1]}'
        return _create_reminder(dt_text, parts[2])

    # 日本語ショートカット: リマインド 2026-03-02 15:00 内容
    m = re.match(r'^リマインド\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+(.+)$', text)
    if m:
        dt_text = f'{m.group(1)} {m.group(2)}'
        return _create_reminder(dt_text, m.group(3))

    # 日本語自然文: 明日15:00に連絡して <内容>
    if '明日' in text and any(k in text for k in ('連絡', 'リマインド', '教えて')):
        hm = re.search(r'明日\s*(\d{1,2})(?::|時)(\d{1,2})?', text)
        if hm:
            hour = int(hm.group(1))
            minute = int(hm.group(2) or 0)
            tomorrow = datetime.now() + timedelta(days=1)
            dt_text = f"{tomorrow.strftime('%Y-%m-%d')} {hour:02d}:{minute:02d}"
            return _create_reminder(dt_text, text)

    if lower == 'remind list':
        items = load_reminders()
        pending = [it for it in items if not it.get('sent')]
        if not pending:
            return True, '未送信リマインドはありません。'
        lines = ['未送信リマインド:']
        for it in sorted(pending, key=lambda x: x.get('run_at', '')):
            lines.append(f"- #{it['id']} {it['run_at']} {it['text']}")
        return True, '\n'.join(lines)

    if lower.startswith('remind done '):
        rid_text = text[len('remind done '):].strip()
        if not rid_text.isdigit():
            return True, '使い方: `remind done <id>`'
        rid = int(rid_text)
        items = load_reminders()
        found = False
        for it in items:
            if int(it.get('id', 0)) == rid:
                it['sent'] = True
                found = True
                break
        if not found:
            return True, f'#{rid} は見つからなかったよ。'
        save_reminders(items)
        task = reminder_tasks.get(rid)
        if task and not task.done():
            task.cancel()
        return True, f'#{rid} を完了扱いにしたよ。'

    return False, ''


async def run_one_time_reminder():
    global one_time_reminder_sent
    target_dt = parse_reminder_dt(ONE_TIME_REMINDER_AT)
    if not target_dt or one_time_reminder_sent:
        return

    now = datetime.now()
    if target_dt <= now:
        return

    wait_seconds = (target_dt - now).total_seconds()
    await asyncio.sleep(wait_seconds)

    channel_id = ONE_TIME_REMINDER_CHANNEL_ID
    if not channel_id and AUTO_REPLY_CHANNEL_IDS:
        channel_id = sorted(AUTO_REPLY_CHANNEL_IDS)[0]

    if not channel_id:
        return

    channel = client.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.abc.Messageable):
        return

    await channel.send(ONE_TIME_REMINDER_TEXT)
    one_time_reminder_sent = True


async def handle_handoff_command(user_query: str) -> tuple[bool, str]:
    # handoff create <text>
    # handoff pull [N]
    # handoff done <issue_number> [comment]
    # handoff help
    parts = user_query.strip().split(maxsplit=2)
    if not parts or parts[0].lower() != 'handoff':
        return False, ''

    if len(parts) == 1 or parts[1].lower() == 'help':
        help_text = (
            'handoffコマンド:\n'
            '- `handoff create <依頼内容>` 引き継ぎIssueを作成\n'
            '- `handoff pull [件数]` 未処理Issueを表示\n'
            '- `handoff done <番号> [完了メモ]` Issueを完了にする'
        )
        return True, help_text

    action = parts[1].lower()
    if action == 'create':
        if len(parts) < 3 or not parts[2].strip():
            return True, '使い方: `handoff create <依頼内容>`'
        detail = parts[2].strip()
        kind, labels = classify_handoff(detail)
        title = f'[{kind}] {detail.splitlines()[0][:72]}'
        body = textwrap.dedent(
            f"""\
            ## Handoff Request
            {detail}

            ---
            kind: {kind}
            created_by: USAPONIA
            created_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            """
        )
        ok, msg = await asyncio.to_thread(handoff_store.create, title, body, labels)
        return True, msg

    if action == 'pull':
        limit = 5
        if len(parts) >= 3 and parts[2].strip().isdigit():
            limit = int(parts[2].strip())
        ok, msg = await asyncio.to_thread(handoff_store.list_open, limit)
        return True, msg

    if action == 'done':
        if len(parts) < 3:
            return True, '使い方: `handoff done <番号> [完了メモ]`'
        done_parts = parts[2].split(maxsplit=1)
        if not done_parts[0].isdigit():
            return True, 'Issue番号は数字で指定してください。例: `handoff done 12 実装完了`'
        issue_number = int(done_parts[0])
        note = done_parts[1] if len(done_parts) > 1 else ''
        ok, msg = await asyncio.to_thread(handoff_store.close, issue_number, note)
        return True, msg

    return True, '不明なhandoff操作です。`handoff help` を使ってください。'


def is_handoff_help_request(user_query: str) -> bool:
    q = user_query.lower()
    return (
        ('handoff' in q and any(k in q for k in ('使い方', 'わから', '忘れ', 'help')))
        or ('引き継ぎ' in q and any(k in q for k in ('使い方', 'わから', '忘れ')))
    )


def should_suggest_handoff(user_query: str, channel_id: int) -> bool:
    q = user_query.lower()
    # handoff自体の操作中や短文の相槌では提案しない
    if 'handoff' in q or len(user_query.strip()) < 12:
        return False

    # 「あとでやる」「計画」「仕様」「タスク」など、引き継ぎ忘れが起きやすい文脈で提案
    topic_hit = any(
        k in q for k in (
            'あとで', '明日', '計画', '仕様', 'タスク', 'やること',
            'メモ', '実装', '案', 'アイデア', '忘れそう', '後で'
        )
    )
    if not topic_hit:
        return False

    # 20分に1回まで
    now = time.time()
    last = last_handoff_suggest_at.get(channel_id, 0)
    if now - last < 20 * 60:
        return False
    return True


def handoff_help_text() -> str:
    return (
        '引き継ぎの使い方:\n'
        '- `handoff create <依頼内容>` 共有メモ作成\n'
        '- `handoff pull` 未処理一覧\n'
        '- `handoff done <番号> <完了メモ>` 完了\n'
        '困ったら `handoff help` と送ってね。'
    )


def handoff_suggestion_text() -> str:
    return 'この内容、忘れる前にGitHubへ共有しましょうか？ 共有するなら `handoff create <内容>` と送ってね。'


WEATHER_CODE_MAP = {
    0: '快晴',
    1: '晴れ',
    2: '晴れ時々くもり',
    3: 'くもり',
    45: '霧',
    48: '霧',
    51: '弱い霧雨',
    53: '霧雨',
    55: '強い霧雨',
    56: '凍る霧雨',
    57: '凍る強い霧雨',
    61: '弱い雨',
    63: '雨',
    65: '強い雨',
    66: '弱い着氷性の雨',
    67: '強い着氷性の雨',
    71: '弱い雪',
    73: '雪',
    75: '強い雪',
    77: '雪の粒',
    80: 'にわか雨',
    81: 'やや強いにわか雨',
    82: '強いにわか雨',
    85: 'にわか雪',
    86: '強いにわか雪',
    95: '雷雨',
    96: '雷雨（ひょうの可能性）',
    99: '激しい雷雨（ひょうの可能性）',
}


def weather_code_text(code: int) -> str:
    return WEATHER_CODE_MAP.get(int(code), '不明')


def parse_weather_request(user_query: str) -> tuple[str, int, int, str] | None:
    q = user_query.strip()
    lq = q.lower()
    if '天気' not in q and 'weather' not in lq:
        return None

    location = DEFAULT_WEATHER_LOCATION
    m = re.search(r'(.+?)の(?:今日|明日|明後日|あさって|3日(?:間)?|三日(?:間)?|1週間|週間)?天気', q)
    if m:
        candidate = m.group(1).strip().strip('、,')
        if candidate and candidate not in ('ここ', 'このへん', 'この辺'):
            location = candidate

    offset = 0
    label = '今日'
    if '明後日' in q or 'あさって' in q:
        offset = 2
        label = '明後日'
    elif '明日' in q:
        offset = 1
        label = '明日'

    days = 1
    if any(k in q for k in ('3日', '3日間', '三日', '三日間')):
        days = 3
        label = '3日'
    elif any(k in q for k in ('1週間', '週間', '7日', '7日間')):
        days = 7
        label = '1週間'

    return location, offset, days, label


def fetch_weather_reply(user_query: str) -> tuple[bool, str]:
    parsed = parse_weather_request(user_query)
    if not parsed:
        return False, ''
    location, offset, days, label = parsed

    try:
        geo = requests.get(
            'https://geocoding-api.open-meteo.com/v1/search',
            params={
                'name': location,
                'count': 1,
                'language': 'ja',
                'format': 'json',
            },
            timeout=10,
        )
        geo.raise_for_status()
        results = (geo.json() or {}).get('results') or []
        if not results:
            return True, f'その場所は見つからなかった。場所名をもう少し具体的にして。例: 愛知県岡崎市'

        top = results[0]
        lat = top['latitude']
        lon = top['longitude']
        resolved_name = top.get('name', location)
        admin1 = top.get('admin1', '')
        admin2 = top.get('admin2', '')
        area_text = ' '.join([x for x in (admin1, admin2, resolved_name) if x]).strip()

        need_days = max(days, offset + 1)
        fc = requests.get(
            'https://api.open-meteo.com/v1/forecast',
            params={
                'latitude': lat,
                'longitude': lon,
                'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max',
                'forecast_days': need_days,
                'timezone': 'Asia/Tokyo',
            },
            timeout=10,
        )
        fc.raise_for_status()
        daily = (fc.json() or {}).get('daily') or {}
        codes = daily.get('weather_code') or []
        tmax = daily.get('temperature_2m_max') or []
        tmin = daily.get('temperature_2m_min') or []
        pop = daily.get('precipitation_probability_max') or []

        if not codes:
            return True, '天気データの取得に失敗した。少し待ってもう一度試して。'

        if days == 1:
            idx = min(offset, len(codes) - 1)
            desc = weather_code_text(codes[idx])
            hi = round(float(tmax[idx])) if idx < len(tmax) else '-'
            lo = round(float(tmin[idx])) if idx < len(tmin) else '-'
            rain = round(float(pop[idx])) if idx < len(pop) else 0
            reply = (
                f'結論。{area_text}の{label}は「{desc}」。\n'
                f'最高{hi}℃ / 最低{lo}℃、降水確率の目安は{rain}%。\n'
                '外出なら気温差だけ気をつけて。'
            )
            return True, reply

        lines = [f'結論。{area_text}の{label}予報。']
        day_names = ['今日', '明日', '明後日', '3日後', '4日後', '5日後', '6日後']
        for i in range(min(days, len(codes))):
            desc = weather_code_text(codes[i])
            hi = round(float(tmax[i])) if i < len(tmax) else '-'
            lo = round(float(tmin[i])) if i < len(tmin) else '-'
            rain = round(float(pop[i])) if i < len(pop) else 0
            lines.append(f'{day_names[i]}: {desc} / {hi}℃-{lo}℃ / 降水{rain}%')
        lines.append('必要なら時間帯別も出せる。')
        return True, '\n'.join(lines)

    except Exception:
        return True, '天気の取得に失敗した。ネットワークかAPI側の一時エラーかも。少し待って再試行して。'


async def handle_weather_query(user_query: str) -> tuple[bool, str]:
    parsed = parse_weather_request(user_query)
    if not parsed:
        return False, ''
    return await asyncio.to_thread(fetch_weather_reply, user_query)


def is_confirm_share_message(text: str) -> bool:
    q = text.strip().lower()
    return any(k in q for k in ('共有して', '共有する', 'はい', 'ok', 'お願い', 'issue化'))


def is_image_attachment(att: discord.Attachment) -> bool:
    if att.content_type and att.content_type.startswith('image/'):
        return True
    filename = (att.filename or '').lower()
    return filename.endswith(('.png', '.jpg', '.jpeg', '.webp', '.heic'))


async def handle_image_note_handoff(message: discord.Message, user_query: str) -> tuple[bool, str]:
    key = (message.channel.id, message.author.id)

    # 直前の画像メモがあり、今回が共有確定メッセージならIssue化
    pending = pending_image_handoff.get(key)
    if pending and is_confirm_share_message(user_query):
        ok, msg = await asyncio.to_thread(
            handoff_store.create,
            pending['title'],
            pending['body'],
            pending['labels'],
        )
        pending_image_handoff.pop(key, None)
        return True, msg

    image_attachments = [a for a in message.attachments if is_image_attachment(a)]
    if not image_attachments:
        return False, ''

    first = image_attachments[0]
    image_bytes = await first.read(use_cached=True)
    mime_type = first.content_type or 'image/png'

    try:
        ocr_text = adapter.extract_text_from_image(image_bytes, mime_type)
    except Exception as e:
        return True, f'画像の読み取りに失敗したよ: {e}'

    if not ocr_text:
        return True, '画像から文字を読み取れなかったよ。もう少し鮮明な写真で試してみて。'

    kind, labels = classify_handoff(ocr_text)
    title = f'[{kind}] {ocr_text.splitlines()[0][:72]}'
    body = textwrap.dedent(
        f"""\
        ## Handoff Request (from image note)
        {ocr_text}

        ---
        kind: {kind}
        source: discord_image
        image_name: {first.filename}
        created_by: USAPONIA
        created_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """
    )

    if is_confirm_share_message(user_query):
        ok, msg = await asyncio.to_thread(handoff_store.create, title, body, labels)
        return True, f'画像メモを読み取って分類したよ（{kind}）。\n{msg}'

    pending_image_handoff[key] = {'title': title, 'body': body, 'labels': labels, 'kind': kind}
    preview = '\n'.join(ocr_text.splitlines()[:6]).strip()
    return True, (
        f'画像メモを読み取ったよ。分類は `{kind}` です。\n'
        f'--- OCRプレビュー ---\n{preview}\n'
        f'---\n'
        'GitHubへ共有する？ 共有するなら「はい、共有して」と送ってね。'
    )


@client.event
async def on_ready():
    global one_time_reminder_task
    print(f'ウサポニアが {client.user} としてログインしました！')
    if AUTO_REPLY_CHANNEL_IDS:
        print(f'自動反応チャンネル: {sorted(AUTO_REPLY_CHANNEL_IDS)}')
    else:
        print('自動反応チャンネル: 未設定（USAPONIA_AUTO_CHANNEL_IDS を設定してください）')
    print(f'LLMバックエンド: {LLM_BACKEND}')
    print(f'handoffプロバイダ: {HANDOFF_PROVIDER}')
    print(f'コマンド実行: {"ON" if ENABLE_COMMAND_EXECUTION else "OFF"} / DRY_RUN: {"ON" if DRY_RUN else "OFF"}')
    if PROGRESS_NOTIFY:
        print(f'進捗通知: ON / チャンネルID: {PROGRESS_CHANNEL_ID if PROGRESS_CHANNEL_ID else "返信先と同じ"}')
    else:
        print('進捗通知: OFF')
    if parse_reminder_dt(ONE_TIME_REMINDER_AT):
        print(f'1回リマインド予約: {ONE_TIME_REMINDER_AT}')
        if one_time_reminder_task is None or one_time_reminder_task.done():
            one_time_reminder_task = asyncio.create_task(run_one_time_reminder())
    schedule_pending_reminders()
    pending_count = len([x for x in load_reminders() if not x.get('sent')])
    print(f'保存済みリマインド: {pending_count}件')


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    is_prefixed = message.content.startswith('!usapon')
    is_auto_channel = message.channel.id in AUTO_REPLY_CHANNEL_IDS
    if not is_prefixed and not is_auto_channel:
        return

    user_query = message.content.replace('!usapon', '', 1).strip() if is_prefixed else message.content.strip()
    has_image = any(is_image_attachment(a) for a in message.attachments)
    if not user_query and not has_image:
        await message.channel.send('相談内容をもう少し詳しく教えてね。')
        return

    weather_handled, weather_message = await handle_weather_query(user_query)
    if weather_handled:
        await message.channel.send(weather_message)
        memory_store.append('BOT', message.channel.id, weather_message)
        return

    remind_handled, remind_message = await handle_reminder_command(user_query, message.channel.id)
    if remind_handled:
        await message.channel.send(remind_message)
        memory_store.append('BOT', message.channel.id, remind_message)
        return

    image_handled, image_message = await handle_image_note_handoff(message, user_query)
    if image_handled:
        await message.channel.send(image_message)
        memory_store.append('BOT', message.channel.id, image_message)
        return

    if is_handoff_help_request(user_query):
        help_msg = handoff_help_text()
        await message.channel.send(help_msg)
        memory_store.append('BOT', message.channel.id, help_msg)
        return

    handoff_handled, handoff_message = await handle_handoff_command(user_query)
    if handoff_handled:
        await message.channel.send(handoff_message)
        memory_store.append('BOT', message.channel.id, handoff_message)
        return

    job_id = uuid4().hex[:8]
    await send_progress(message, job_id, '受付', '依頼を受け取りました。')

    memory_context = memory_store.load()
    memory_store.append('USER', message.channel.id, user_query)

    async with message.channel.typing():
        try:
            await send_progress(message, job_id, '生成中', 'AIで回答プランを作成しています。')
            model_text = adapter.generate(build_prompt(user_query, memory_context))
            parsed = parse_model_output(model_text)
            if parsed.mode == 'CHAT' and is_weak_chat_reply(parsed.reply):
                retry_text = adapter.generate(
                    build_force_answer_prompt(user_query, parsed.reply, memory_context)
                )
                retry_parsed = parse_model_output(retry_text)
                if retry_parsed.reply:
                    parsed = retry_parsed

            if parsed.mode == 'COMMAND' and parsed.command:
                await send_progress(message, job_id, '実行中', f'コマンドを実行します: {parsed.command}')
                ok, executed_command, output = command_executor.run(parsed.command, user_query)
                status_emoji = '✅' if ok else '⚠️'
                bot_message = f"{parsed.reply}\n\n{status_emoji} 実行結果:\n{output}"
                await message.channel.send(bot_message)
                memory_store.append('BOT', message.channel.id, f"{parsed.reply} | CMD={executed_command} | OUT={output[:1000]}")
                await send_progress(message, job_id, '完了', 'コマンド実行まで完了しました。')
                return

            if parsed.mode == 'PATCH':
                patch_msg = format_patch_reply(parsed.reply, parsed.patch)
                if should_suggest_handoff(user_query, message.channel.id):
                    last_handoff_suggest_at[message.channel.id] = time.time()
                    patch_msg = f'{patch_msg}\n\n{handoff_suggestion_text()}'
                await message.channel.send(patch_msg)
                memory_store.append('BOT', message.channel.id, f'{parsed.reply} | PATCH={parsed.patch[:1000]}')
                await send_progress(message, job_id, '完了', 'PATCH提案を返しました。')
                return

            final_reply = parsed.reply
            if should_suggest_handoff(user_query, message.channel.id):
                last_handoff_suggest_at[message.channel.id] = time.time()
                final_reply = f'{final_reply}\n\n{handoff_suggestion_text()}'

            await message.channel.send(final_reply)
            memory_store.append('BOT', message.channel.id, final_reply)
            await send_progress(message, job_id, '完了', '返信を返しました。')

        except genai.errors.APIError as e:
            if is_rate_limit_error(e):
                error_msg = (
                    '⚠️ 今は Gemini API の利用上限に達してるみたい。\n'
                    '数分〜しばらく待ってからもう一度試してね。'
                )
            else:
                error_msg = f'ごめん、Gemini APIエラーが出ちゃった：\n{e}'
            await message.channel.send(error_msg)
            memory_store.append('BOT', message.channel.id, error_msg)
            await send_progress(message, job_id, '失敗', 'Gemini APIエラーが発生しました。')
        except Exception as e:
            error_msg = f'ごめん、エラーが出ちゃった：\n{e}'
            await message.channel.send(error_msg)
            memory_store.append('BOT', message.channel.id, error_msg)
            await send_progress(message, job_id, '失敗', '例外が発生しました。')




async def main():
    # Discordログイン前に、certifi付きSSLコンテキストをHTTPコネクタへ明示注入
    client.http.connector = aiohttp.TCPConnector(limit=0, ssl=DISCORD_SSL_CONTEXT)
    async with client:
        await client.start(DISCORD_TOKEN)


asyncio.run(main())
