# 安裝步驟（給第一次拿到這個專案的人）

請依你的環境選擇一條路：

| | 適合 | 需要 |
|---|---|---|
| **路線 A：Windows 原生** | 直接在 Windows 上執行 | Git、Python 3.10+ |
| **路線 B：WSL** | 習慣在 WSL（Ubuntu）內開發，或不想在 Windows 裝 Python | WSL2 + Ubuntu、Git |

兩條路共用同一份程式（`sync_agent.py`），差別只在安裝方式、路徑寫法與啟動器。

---

## 0. 共同準備：取得 Gemini API Key

到 [Google AI Studio](https://aistudio.google.com/) 申請，稍後填進 `.env`。`.env` 絕對不要上傳到 GitHub。

---

## 路線 A：Windows 原生

### A1. 安裝必要軟體

- [Git](https://git-scm.com/)
- [Python 3.10+](https://www.python.org/)（安裝時記得勾選 "Add Python to PATH"）

### A2. Clone 專案並建立虛擬環境

```bash
git clone https://github.com/chelunchang/KnowledgeFactory.git
cd KnowledgeFactory
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### A3. 設定 `.env`

```bash
copy .env.example .env
```

打開 `.env`，填入：

```
GEMINI_API_KEY=你的金鑰
OUTPUT_DIR=G:\我的雲端硬碟\Notes\YouTube_Notes
```

`OUTPUT_DIR` 改成你要存放筆記的資料夾（建議是 Google Drive 內的資料夾，例如 Obsidian 筆記庫內的某個子資料夾，筆記才會自動同步），資料夾不存在時程式會自動建立。

### A4. 建立雙擊啟動器

1. 把 `launchers\start_windows.bat.example` **複製到專案資料夾以外的地方**（例如 `C:\Users\你的名稱\Scripts`），並改名為 `start_youtube_factory.bat`。
2. 用記事本打開，把 `C:\path\to\your\KnowledgeFactory` 改成你 clone 下來的實際路徑。
3. 對這個 `.bat` 按右鍵 → 傳送到 → 桌面（建立捷徑），之後就能雙擊桌面圖示執行。

> `.bat` 請保持純 ASCII，不要加中文字，否則在繁體中文 Windows 下會亂碼。

### A5. 測試

複製一個 YouTube 影片網址，雙擊啟動器，確認筆記有寫入你設定的 `OUTPUT_DIR`。筆記的**檔名就是影片的原標題**；開頭是程式產生的屬性區塊（`source`、`author`、`published`、`created`、`tags`），內文直接從 `##` 段落開始。

---

## 路線 B：WSL

### B1. 前置

- Windows 已啟用 WSL2 並安裝 Ubuntu。
- 在 Ubuntu 內安裝 Python 的虛擬環境工具：
  ```bash
  sudo apt update && sudo apt install -y python3-venv python3-pip git
  ```

### B2. Clone 專案並建立虛擬環境

**請放在 WSL 的家目錄**（例如 `~/KnowledgeFactory`），不要放在 `/mnt/c/...`，那邊的檔案存取很慢。

```bash
git clone https://github.com/chelunchang/KnowledgeFactory.git ~/KnowledgeFactory
cd ~/KnowledgeFactory
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### B3. 設定 `.env`

```bash
cp .env.example .env
```

打開 `.env`，填入：

```
GEMINI_API_KEY=你的金鑰
OUTPUT_DIR=/mnt/g/我的雲端硬碟/Notes/YouTube_Notes
```

**注意：在 WSL 內路徑要寫 Linux 格式**。Windows 的 `G:\Notes` 要寫成 `/mnt/g/Notes`，寫成 `G:\...` 程式會直接報錯。

### B4. 掛載 Google Drive（G:）

WSL 不會自動掛載 Google Drive，要先掛載一次，並設成開機自動掛載：

```bash
sudo mkdir -p /mnt/g
echo 'G: /mnt/g drvfs defaults,nofail 0 0' | sudo tee -a /etc/fstab
sudo mount -a
```

> 如果 Google Drive 比 WSL 晚啟動而沒掛上，程式會偵測到並中止（不會寫到錯的地方）。確認 Drive 已啟動後執行 `sudo mount -a` 再重跑即可。

### B5. 測試（直接帶網址）

```bash
.venv/bin/python sync_agent.py "https://www.youtube.com/watch?v=影片ID"
```

完成後，到 `OUTPUT_DIR` 找筆記：**檔名就是影片的原標題**；開頭是程式產生的屬性區塊（`source`、`author`、`published`、`created`、`tags`），內文直接從 `##` 段落開始。

### B6. 建立雙擊啟動器（從 Windows 雙擊、在 WSL 內執行）

1. 把 `launchers\start_wsl.bat.example` 複製到專案以外的地方（例如 `C:\Users\你的名稱\Scripts`），改名為 `start_youtube_factory_wsl.bat`。
2. 打開檔案，把 `Ubuntu-24.04` 換成你的 WSL 發行版名稱（用 `wsl -l -q` 查看），`~/KnowledgeFactory` 換成你在 WSL 內的專案路徑。
3. 對它按右鍵 → 傳送到 → 桌面（建立捷徑）。
4. 複製一個 YouTube 網址，雙擊執行。WSL 版會用 `powershell.exe Get-Clipboard` 讀取 Windows 的剪貼簿。

---

## 之後：更新與疑難排解

- 更新程式：`git pull`；套件有變動時再執行一次 `pip install -r requirements.txt`。
- 錯誤訊息、額度、模型相關問題請看 [README.md](README.md) 的「疑難排解」。
