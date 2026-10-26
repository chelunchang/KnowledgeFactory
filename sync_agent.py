from dotenv import load_dotenv
load_dotenv()  # 安全讀取 .env 金鑰

import os
import sys
import asyncio
import datetime
import platform
import re
import subprocess
import unicodedata
import requests
from google.antigravity import Agent, LocalAgentConfig
from youtube_transcript_api import YouTubeTranscriptApi

# Gemini 速率限制 / 伺服器過載重試設定
MAX_RETRIES = 4
RETRY_BASE_DELAY = 8  # 秒，每次重試延遲會倍增 (8s → 16s → 32s → 64s)
CHAT_TIMEOUT_SECONDS = 600  # 單次呼叫逾時秒數，避免連線卡死卻不拋例外導致無限期等待


async def chat_with_retry(config, prompt, **kwargs):
    """呼叫 Gemini 時自動處理速率限制 / 伺服器過載等暫時性錯誤，採用指數退避重試。

    agent.chat() 本身是 lazy streaming，幾乎瞬間返回；真正會等待、
    也真正可能拋出 503 等錯誤的地方是消費串流的 .text()，因此重試範圍
    必須包含 .text()，否則重試形同虛設。

    一旦底層連線在串流中途出錯，該連線本身通常已經關閉，沿用同一個
    Agent 物件重試只會立刻得到「連線已關閉」的錯誤；因此每次嘗試都
    重新建立一個全新的 Agent 連線。
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with Agent(config) as agent:
                response = await agent.chat(prompt, **kwargs)
                return await asyncio.wait_for(response.text(), timeout=CHAT_TIMEOUT_SECONDS)
        except Exception as e:
            last_error = e
            err_text = str(e).lower()
            if isinstance(e, asyncio.TimeoutError):
                err_text = f"timeout：等待 Gemini 回應超過 {CHAT_TIMEOUT_SECONDS} 秒"
            # 涵蓋：429 速率限制 / 503 伺服器過載("high demand"、"unavailable") /
            # 暫時性逾時 / 串流中途連線被關閉（通常是前述錯誤的連帶後果）
            # 每日額度用盡（quotaId 含 "perday"）屬於非暫時性錯誤，重試也沒用，必須直接放棄
            is_daily_quota_exhausted = "perday" in err_text
            is_retryable = (not is_daily_quota_exhausted) and any(keyword in err_text for keyword in [
                "429", "rate", "quota",
                "503", "unavailable", "high demand", "overloaded",
                "deadline exceeded", "timeout",
                "connection closed", "1000 (ok)",
            ])
            if is_daily_quota_exhausted:
                last_error.attempts_made = attempt
                last_error.daily_quota_exhausted = True
                raise last_error
            if is_retryable and attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                print(f"⚠️ 偵測到暫時性錯誤（第 {attempt} 次嘗試）：{e or err_text}")
                print(f"   {delay} 秒後自動重試...")
                await asyncio.sleep(delay)
                continue
            # 把實際重試次數標記在例外訊息上，避免外層印出誤導性的「已重試 N 次」
            last_error.attempts_made = attempt
            raise last_error

def get_video_id(url):
    """從網址自動解析出 YouTube 的 Video ID"""
    if "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0].split("#")[0]
    elif "v=" in url:
        return url.split("v=")[1].split("&")[0].split("#")[0]
    return None

def get_video_info(video_id: str) -> dict:
    """取得影片原標題、頻道名稱（YouTube oEmbed，不需要 API Key）與發布日（解析影片頁面）。
    取不到的欄位為 None，不會讓整個流程失敗。"""
    info = {"title": None, "author": None, "published": None}
    watch = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r = requests.get("https://www.youtube.com/oembed", params={"url": watch, "format": "json"}, timeout=10)
        r.raise_for_status()
        j = r.json()
        info["title"] = (j.get("title") or "").strip() or None
        info["author"] = (j.get("author_name") or "").strip() or None
    except Exception:
        pass
    try:
        r = requests.get(watch, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-TW,zh;q=0.9"}, timeout=10)
        m = re.search(r'<meta itemprop="datePublished" content="(\d{4}-\d{2}-\d{2})', r.text)
        if m:
            info["published"] = m.group(1)
    except Exception:
        pass
    return info

def safe_filename(title: str, max_chars: int = 100, max_bytes: int = 240) -> str:
    """把影片標題轉成可用的檔名：去掉檔名不能用的字元，並限制長度
    （避開 Windows 路徑長度上限，以及 Linux 單一檔名 255 bytes 的上限）"""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title)
    name = re.sub(r"\s+", " ", name).strip(" .")[:max_chars]
    while len(name.encode("utf-8")) > max_bytes:
        name = name[:-1]
    return name.strip(" .") or "untitled"

# ── 設定：個人化的部分都放在 .env，不要寫死在程式裡 ──────────────────────────
# GEMINI_API_KEY  必填，Gemini API Key
# OUTPUT_DIR      必填，筆記輸出資料夾（建議指到 Google Drive。Windows 例：G:\我的雲端硬碟\Notes\YouTube_Notes；WSL 例：/mnt/g/我的雲端硬碟/Notes/YouTube_Notes）
# GEMINI_MODEL    選填，預設 gemini-3.5-flash（明確指定模型，避免 SDK 升級後預設模型改變）
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "").strip()

def require_output_dir() -> str:
    if not OUTPUT_DIR:
        print("\n❌ 【設定不完整】尚未設定 OUTPUT_DIR。")
        print("💡 請在 .env 加入一行，例如：")
        print("   Windows: OUTPUT_DIR=G:\\我的雲端硬碟\\Notes\\YouTube_Notes")
        print("   WSL:     OUTPUT_DIR=/mnt/g/我的雲端硬碟/Notes/YouTube_Notes")
        sys.exit(1)
    return OUTPUT_DIR

def ensure_output_mounted(output_dir: str) -> None:
    # 路徑在 /mnt/<磁碟>/ 底下時，該磁碟必須真的掛載；沒掛載時它只是 WSL 內的空資料夾，
    # 直接寫入會靜默寫到錯的地方，所以要先擋下來。
    if is_wsl() and re.match(r"^[A-Za-z]:[\\/]", output_dir):
        print(f"\n❌ 【設定錯誤】在 WSL 內 OUTPUT_DIR 不能用 Windows 路徑：{output_dir}")
        print("💡 請改成 /mnt/<磁碟代號>/... 的寫法，例如 G:\\Notes 要寫成 /mnt/g/Notes")
        sys.exit(1)
    parts = output_dir.split("/")
    if output_dir.startswith("/mnt/") and len(parts) > 2 and parts[2]:
        mount_point = "/mnt/" + parts[2]
        if not os.path.ismount(mount_point):
            print(f"\n❌ 【安全中斷】{mount_point} 尚未掛載，筆記不會寫入你設定的資料夾。")
            print(f"💡 請確認磁碟或雲端硬碟已啟動，執行 sudo mount -a（或 sudo mount -t drvfs <磁碟代號>: {mount_point}）後再重新執行。")
            sys.exit(1)

def strip_code_fence(text: str) -> str:
    # Gemini 有時會把整份筆記包在 ```markdown ... ``` 裡（有時圍欄後還附上「工作報告」之類的說明），
    # Obsidian 會把它當成程式碼區塊顯示。圍欄行數為偶數代表外層有正確閉合，才去除外層與後面的說明；
    # 筆記內部本來就有的程式碼區塊（例如 mermaid）不受影響。
    lines = text.strip().splitlines()
    fence_idx = [i for i, l in enumerate(lines) if l.strip().startswith("```")]
    if (len(fence_idx) >= 2 and len(fence_idx) % 2 == 0 and fence_idx[0] == 0
            and re.match(r"^```(markdown|md)?\s*$", lines[0].strip(), re.I)):
        return "\n".join(lines[1:fence_idx[-1]]).strip() + "\n"
    return text

def strip_leading_h1(text: str) -> str:
    # 筆記標題由檔名（影片原標題）提供；若模型仍自己加了 # 一級標題，把開頭那一行移除，
    # 標題後面緊接的分隔線（---）也一併移除，避免筆記一開頭就是一條橫線。
    lines = text.lstrip("\n").split("\n")
    if lines and re.match(r"^#\s+\S", lines[0]):
        lines = "\n".join(lines[1:]).lstrip("\n").split("\n")
        if lines and re.match(r"^(-{3,}|\*{3,}|_{3,})\s*$", lines[0]):
            lines = lines[1:]
    return "\n".join(lines).lstrip("\n")

def yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

def build_frontmatter(video_id: str, author, created: str, published=None, tags=()) -> str:
    """產生 Obsidian 屬性區塊，格式比照 Web Clipper：source / author / published / created / tags"""
    lines = ["---", f"source: {yaml_quote('https://www.youtube.com/watch?v=' + video_id)}"]
    name = re.sub(r"[\[\]#^|\\/]", " ", author or "").strip()  # 雙向連結不能含這些字元
    if name:
        lines += ["author:", f"  - {yaml_quote('[[' + name + ']]')}"]
    if published:
        lines.append(f"published: {published}")
    lines.append(f"created: {created}")
    if tags:
        lines.append("tags:")
        lines += [f"  - {t}" for t in tags]
    lines.append("---")
    return "\n".join(lines) + "\n"  # 結尾 --- 之後直接接內文，不多留空行（與 Web Clipper 一致）

def strip_frontmatter(text: str) -> str:
    # 屬性區塊由程式產生；若模型仍自己輸出了 YAML 屬性區塊（以 --- 包起來），把它移除。
    # 只有內容確實像 YAML 時才移除，避免誤刪開頭的分隔線。
    lines = text.lstrip("\n").split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 40)):
            if lines[i].strip() == "---":
                inner = lines[1:i]
                if all(not l.strip() or re.match(r"^[\w\-]+\s*:", l) or re.match(r"^(\s+|-\s)", l) for l in inner):
                    return "\n".join(lines[i + 1:]).lstrip("\n")
                break
    return text

def extract_tags(text: str):
    """模型依影片內容，在筆記最後以「標籤：a, b, c」一行提供標籤。
    取出標籤並把那一行從筆記移除；回傳 (筆記內容, 標籤清單)。找不到就回傳 (原文, [])。"""
    lines = text.rstrip().split("\n")
    for idx in range(len(lines) - 1, max(len(lines) - 4, -1), -1):  # 只看最後 3 行
        m = re.match(r"^\s*(?:標籤|tags)\s*[:：]\s*(.+?)\s*$", lines[idx], re.I)
        if not m:
            continue
        tags = []
        for raw in re.split(r"[,，、;；]", m.group(1)):
            t = re.sub(r"\s+", "-", raw.replace("#", "").strip())
            t = re.sub(r"[^\w\-/]", "", t).strip("-")  # Obsidian 標籤不能含空白與多數符號
            if t and t not in tags:
                tags.append(t)
        return "\n".join(lines[:idx]).rstrip() + "\n", tags[:6]
    return text, []

# 裝飾性 emoji。箭頭（→ ➔ ➡ 等）、★ ☆ ♀ ♂ ♪ ♫ 常用來表達內容，所以保留。
_KEEP_SYMBOLS = set("★☆♀♂♪♫")
_EMOJI_SINGLES = {0x2705, 0x274C, 0x274E, 0x2728, 0x2753, 0x2754, 0x2755, 0x2757, 0x2764, 0x2744, 0x2747,
                  0x2B50, 0x2B55, 0x2B1B, 0x2B1C, 0x231A, 0x231B, 0x3030, 0x303D, 0x3297, 0x3299,
                  0xFE0F, 0x200D, 0x20E3}

def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    if ch in _KEEP_SYMBOLS:
        return False
    return (0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x26FF or 0x23E9 <= o <= 0x23FA or o in _EMOJI_SINGLES)

def strip_emoji(text: str) -> str:
    """移除裝飾性 emoji；程式碼區塊（```）內的內容不動，並整理被移除後留下的多餘空白。"""
    out, in_code = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if not in_code:
            new = "".join(ch for ch in line if not _is_emoji(ch))
            if new != line:
                indent = re.match(r"^\s*", line).group(0)
                line = indent + re.sub(r" {2,}", " ", new.strip())
        out.append(line)
    return "\n".join(out)

def to_cn_number(n: int) -> str:
    digits = "零一二三四五六七八九"
    if n < 10:
        return digits[n]
    if n < 20:
        return "十" + (digits[n % 10] if n % 10 else "")
    if n < 100:
        return digits[n // 10] + "十" + (digits[n % 10] if n % 10 else "")
    return str(n)

# 標題開頭既有的序號：一、 (一) (1) 1. 01. 1、 1)
_NUM_PREFIX = re.compile(r"^(?:[一二三四五六七八九十百]+[、．.]|[(（][一二三四五六七八九十]+[)）]|[(（]\d+[)）]|\d+[.．、)）](?!\d))\s*")

def normalize_headings(text: str) -> str:
    """統一標題序號：## 一、 / ### (一) / #### 1. / ##### (1)（程式碼區塊內不動）。
    原本標題上的序號會先去掉再依出現順序重編，同一層級每遇到上一層的新標題就從頭編起。"""
    counters = [0, 0, 0, 0]
    out, in_code = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        m = None if in_code else re.match(r"^(#{2,5})\s+(.*\S)\s*$", line)
        if not m:
            out.append(line)
            continue
        level = len(m.group(1)) - 2
        title = _NUM_PREFIX.sub("", m.group(2), count=1).strip()
        if not title:
            out.append(line)
            continue
        counters[level] += 1
        for i in range(level + 1, 4):
            counters[i] = 0
        n = counters[level]
        label = [f"{to_cn_number(n)}、", f"({to_cn_number(n)})", f"{n}. ", f"({n}) "][level]
        out.append(f"{m.group(1)} {label}{title}")
    return "\n".join(out)

def _is_punct(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "PS"

def _is_word(ch: str) -> bool:
    return bool(ch) and not ch.isspace() and not _is_punct(ch)

_PAIRS = {"「": "」", "『": "』", "（": "）", "(": ")", "【": "】", "《": "》", "〈": "〉",
          "“": "”", "‘": "’", "[": "]", "［": "］"}

def fix_bold_punctuation(text: str) -> str:
    """修正中文常見的粗體失效：**「窗口點餐」**的 這種寫法，** 內側緊貼標點、外側緊貼文字時，
    Markdown 不會判定為粗體，筆記裡會直接看到 ** 符號。處理方式：
    - 整段被一對引號／括號包住：把那一對標點移到 ** 外面（「**窗口點餐**」的）
    - 只有一端緊貼標點：在 ** 外側補一個空白（**留客（Retention）** 與）
    文字內容與順序不變。程式碼區塊內不動。"""
    out, in_code = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code or "**" not in line:
            out.append(line)
            continue

        def repl(m):
            before = line[m.start() - 1] if m.start() > 0 else ""
            after = line[m.end()] if m.end() < len(line) else ""
            c = m.group(1)
            open_bad = _is_word(before) and bool(c) and _is_punct(c[0])
            close_bad = _is_word(after) and bool(c) and _is_punct(c[-1])
            if not (open_bad or close_bad):
                return m.group(0)
            if len(c) > 2 and _PAIRS.get(c[0]) == c[-1]:  # 整段被一對引號／括號包住
                return f"{c[0]}**{c[1:-1]}**{c[-1]}"
            return ("" if not open_bad else " ") + f"**{c}**" + ("" if not close_bad else " ")

        out.append(re.sub(r"\*\*(?!\s)(.+?)(?<!\s)\*\*", repl, line))
    return "\n".join(out)

def is_wsl() -> bool:
    return "microsoft" in platform.uname().release.lower()

def read_clipboard() -> str:
    # 優先順序：命令列帶入的網址 → WSL 內讀取 Windows 剪貼簿 → 其他系統用 pyperclip
    if len(sys.argv) > 1:
        return sys.argv[1].strip()
    if is_wsl():
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
        )
        return out.stdout.strip()
    import pyperclip  # Windows / macOS / Linux 桌面環境
    return pyperclip.paste().strip()

async def main():
    ensure_output_mounted(require_output_dir())  # 先檢查，避免白白消耗 Gemini 額度
    # =========================================================================
    # 🎯 核心：自動從剪貼簿讀取你剛剛按 Ctrl+C 複製的網址（也可在命令列直接帶網址）
    # =========================================================================
    target_video_url = read_clipboard()
    
    # 🚨 安全檢查：確保剪貼簿裡的是 YouTube 網址
    if "youtube.com" not in target_video_url and "youtu.be" not in target_video_url:
        print("\n❌ 【安全中斷】偵測失敗！")
        print(f"👉 當前剪貼簿內容不是 YouTube 網址：\n   \"{target_video_url}\"")
        print("💡 請先去瀏覽器複製正確的影片網址後，再點擊執行。")
        sys.exit(1)
        
    print(f"\n🚀 成功取得網址: {target_video_url}")
    
    video_id = get_video_id(target_video_url)
    if not video_id:
        print("❌ 無法解析影片 ID，請檢查網址格式是否正確。")
        sys.exit(1)

    info = get_video_info(video_id)
    video_title = info["title"]
    if video_title:
        print(f"🏷️ 影片標題: {video_title}")
        print(f"📺 頻道: {info['author'] or '（取不到）'} ｜ 發布日: {info['published'] or '（取不到）'}")
    else:
        video_title = f"YT_{video_id}"
        print(f"⚠️ 無法取得影片標題，檔名改用 {video_title}.md。")

    # ─── 階段一：抓取字幕（新版 1.x instance-based API + 多層 Fallback） ───
    print("⏳ 正在從 YouTube 伺服器下載原始字幕...")
    try:
        # 新版 youtube-transcript-api (1.x) 改為「先建立實例」再呼叫，
        # 舊版的 YouTubeTranscriptApi.list_transcripts(video_id) 已被官方移除。
        ytt_api = YouTubeTranscriptApi()
        transcript_list = ytt_api.list(video_id)
        
        srt_data = None
        # 2. 優先尋找繁體中文、簡體中文或英文的手動字幕
        try:
            srt_data = transcript_list.find_transcript(['zh-TW', 'zh-CN', 'en']).fetch()
        except Exception:
            pass

        # 3. 找不到手動字幕，改抓系統自動產生的中文或英文字幕
        if srt_data is None:
            try:
                srt_data = transcript_list.find_generated_transcript(['zh-TW', 'zh-CN', 'en']).fetch()
            except Exception:
                pass

        # 4. 最終 Fallback：完全不限語言，抓任何一個可用的字幕軌（手動或自動皆可）
        #    這能處理像是日文、韓文等冷門語言頻道沒有中英字幕的情況
        if srt_data is None:
            available = list(transcript_list)
            if available:
                fallback_transcript = available[0]
                print(f"⚠️ 找不到中文/英文字幕，改用偵測到的「{fallback_transcript.language}」字幕（將由 Gemini 一併翻譯整理）。")
                srt_data = fallback_transcript.fetch()
            else:
                raise Exception("此影片完全沒有提供任何語言的字幕軌")

        # 新版 fetch() 回傳的是 FetchedTranscript 物件，內含 snippet 物件（用 .text 屬性）
        # 而非舊版的字典（['text']），這裡統一用 getattr 相容兩種寫法以防萬一。
        raw_text = " ".join([getattr(item, "text", None) or item["text"] for item in srt_data])
        print("💾 字幕下載成功！")
        
    except Exception as e:
        print(f"❌ 抓取字幕失敗（該影片可能未提供字幕、字幕被關閉，或遭到 YouTube 阻擋）：{e}")
        print("💡 建議：確認影片本身在 YouTube 網頁上是否顯示「字幕」按鈕(CC)，沒有 CC 按鈕就無法自動抓取。")
        sys.exit(1)

    # ─── 階段二：呼叫 Gemini 排版摘要 ───
    print("🧠 正在將逐字稿送交 Gemini 進行大腦精煉與結構化排版...")
    config = LocalAgentConfig(
        system_instructions=(
            "你是一個專業的知識整理助理。你將收到一段 YouTube 影片的標題與原始逐字稿，"
            "請整理成工整、純文字風格的筆記（繁體中文，台灣）。"
            "格式要求：1. 標題層級與序號固定如下，最多四層：大主題「## 一、」、中主題「### (一)」、"
            "小主題「#### 1.」、第四層「##### (1)」；序號依出現順序從頭編起，不要跳號。"
            "2. 不要使用任何 emoji 或裝飾符號（標題與內文都不行），也不要使用 callout（> [!…]）與 mermaid 流程圖。"
            "3. 內文以條列為主，條列最多兩層；粗體只用在關鍵詞，且 ** 緊貼的內側不要是標點符號（寫成「**窗口點餐**」，不要寫成 **「窗口點餐」**）。表格只在需要比較多組數字時才使用，其他情況一律改用條列。"
            "4. 第一個大主題固定為「一、內容摘要」，用 3 到 5 句話概述影片重點；"
            "最後一個大主題固定為「結論與重點整理」（序號接續前面的大主題）。"
            "5. 保持邏輯通順，去除重複贅詞。"
            "6. 重要：請直接在回覆內容中輸出完整的 Markdown 筆記文字即可，"
            "不要呼叫任何工具（包括 create_file / edit_file 等檔案操作工具），"
            "也不要嘗試自行建立或寫入任何檔案，檔案儲存將完全由外部程式處理。"
            "7. 不要輸出 # 一級標題（筆記標題由檔名提供），直接從第一個「## 一、」開始，"
            "不要任何開場白、結尾說明或工作報告（例如「以下是為您整理的…」）。"
            "8. 不要輸出 YAML 屬性區塊（以 --- 包起來的 tags、author 等），屬性由外部程式加入。"
            "9. 筆記的最後另起一行，輸出「標籤：」加上 3 到 6 個依影片內容決定的標籤，以半形逗號分隔；"
            "標籤使用繁體中文（專有名詞可保留英文），每個 2 到 8 個字，不含空白與 #，"
            "例如：標籤：品牌策略, 日本戶外, 逆勢成長。這一行是唯一允許出現在筆記結尾的內容。"
        ),
        model=GEMINI_MODEL,
    )
    
    try:
        prompt = f"影片標題：{video_title}\n\n請依規定的格式，將以下逐字稿整理成筆記：\n\n{raw_text}"
        ai_text = await chat_with_retry(config, prompt)
        ai_text = strip_code_fence(ai_text)  # 去掉外層程式碼圍欄，避免 Obsidian 整篇顯示成程式碼
        ai_text = strip_frontmatter(ai_text)  # 屬性區塊改由程式產生，移除模型自己加的
        ai_text, tags = extract_tags(ai_text)  # 標籤由模型依影片內容決定，取出後放進屬性區塊
        print(f"🏷️ 標籤: {', '.join(tags) if tags else '（模型沒有提供，屬性中不寫 tags）'}")
        body = strip_leading_h1(ai_text).rstrip()  # 標題由檔名提供，內文不放 # 一級標題
        body = normalize_headings(strip_emoji(body))  # 統一移除 emoji、重編標題序號（一、/(一)/1./(1)）
        body = fix_bold_punctuation(body)  # 修正「**「…」**」這類不會變粗體的寫法
        ai_text = build_frontmatter(video_id, info["author"], datetime.date.today().isoformat(), info["published"], tags) + body + "\n"

        # ─── 階段三：自動寫入 ───
        output_dir = OUTPUT_DIR
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"{safe_filename(video_title)}.md")  # 檔名 = 影片原標題
        if os.path.exists(output_path):
            print(f"⚠️ 已存在同名檔案，將覆蓋：{os.path.basename(output_path)}")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(ai_text)

        print(f"\n🎉 大功告成！YouTube 筆記已成功寫入：")
        print(f"📍 路徑: {output_path}")
            
    except Exception as e:
        attempts = getattr(e, "attempts_made", 1)
        print(f"❌ Gemini 處理過程中發生錯誤（已嘗試 {attempts} 次仍失敗）：{e}")
        if getattr(e, "daily_quota_exhausted", False):
            print("💡 這是 Gemini 免費方案的「每日」額度已用盡，重試無效，請等隔天額度重置，或前往 https://ai.dev/rate-limit 升級方案。")
        else:
            print("💡 若錯誤訊息包含「high demand」或「503」，代表 Google 伺服器當下過載，純屬暫時性問題，建議幾分鐘後再重新執行一次。")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())