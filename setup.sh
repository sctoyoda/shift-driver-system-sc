#!/bin/bash
# セットアップスクリプト (macOS / Ubuntu)

set -e

echo "=== シフト確認アプリ セットアップ ==="

# ── OS判定 ──
OS="$(uname -s)"

if [ "$OS" = "Darwin" ]; then
  echo "▶ macOS を検出"
  if ! command -v brew &>/dev/null; then
    echo "Homebrew が必要です: https://brew.sh"
    exit 1
  fi
  brew install tesseract tesseract-lang poppler
  # 日本語パック確認
  echo "✅ Tesseract + 日本語パック + Poppler インストール済み"

elif [ "$OS" = "Linux" ]; then
  echo "▶ Linux を検出"
  sudo apt-get update -q
  sudo apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-jpn \
    tesseract-ocr-jpn-vert \
    poppler-utils \
    libgl1
  echo "✅ Tesseract + 日本語パック + Poppler インストール済み"
else
  echo "⚠️  OS を自動判定できませんでした。Tesseract と Poppler を手動でインストールしてください。"
fi

# ── Python 依存パッケージ ──
echo "▶ Python パッケージをインストール中..."
pip install -r requirements.txt

echo ""
echo "=== セットアップ完了 ==="
echo "起動コマンド: streamlit run app.py"
