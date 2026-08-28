#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load from local untracked .env if present
if [ -f "${REPO_DIR}/.env" ]; then
    export $(grep -v '^#' "${REPO_DIR}/.env" | xargs)
fi

WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"

if [ -z "$WEBHOOK_URL" ]; then
    echo "✖ Error: DISCORD_WEBHOOK_URL is not configured."
    echo "Please define DISCORD_WEBHOOK_URL in ${REPO_DIR}/.env or export it in your shell:"
    echo "  export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'"
    exit 1
fi

# Detect version from python package if not provided
DEFAULT_VER="v$(python3 -c "import wavecontroller; print(wavecontroller.__version__)" 2>/dev/null || echo "0.1.0-prealpha.5")"
VERSION="${1:-$DEFAULT_VER}"

# If changes not provided, gather recent git commits as bullet points
if [ -n "$2" ]; then
    CHANGES="$2"
else
    CHANGES=$(git -C "$REPO_DIR" log -n 5 --pretty=format:"• %s" 2>/dev/null || echo "• Maintenance and feature updates.")
fi

echo "Broadcasting release announcement for ${VERSION} to Discord..."

PAYLOAD=$(jq -n \
  --arg tag "$VERSION" \
  --arg title "🚀 WaveController Release: $VERSION" \
  --arg url "https://github.com/oparada1988/WaveController/releases" \
  --arg changes "$CHANGES" \
  '{
    username: "WaveController Releases",
    avatar_url: "https://raw.githubusercontent.com/oparada1988/WaveController/main/assets/icons/WaveController.png",
    embeds: [
      {
        title: $title,
        url: $url,
        description: ("An official update for **WaveController** is now available!\n\n### 📝 What'\''s Changed\n" + $changes),
        color: 8141549,
        fields: [
          {
            name: "📦 Version",
            value: ("`" + $tag + "`"),
            inline: true
          },
          {
            name: "🏷️ Channel",
            value: "Alpha Release",
            inline: true
          },
          {
            name: "⚡ How to Upgrade",
            value: "```bash\ncd ~/WaveController && ./install.sh --upgrade\n```",
            inline: false
          },
          {
            name: "🔗 Useful Links",
            value: ("[Release Page](" + $url + ") • [GitHub Repository](https://github.com/oparada1988/WaveController) • [Technical Architecture](https://github.com/oparada1988/WaveController/blob/main/docs/WaveController_Elgato_Hardware_Technical_Architecture.md)"),
            inline: false
          }
        ],
        thumbnail: {
          url: "https://raw.githubusercontent.com/oparada1988/WaveController/main/assets/icons/WaveController.png"
        },
        footer: {
          text: "WaveController • Linux Multi-Track Audio Engine & Hardware Manager",
          icon_url: "https://raw.githubusercontent.com/oparada1988/WaveController/main/assets/icons/WaveController.png"
        },
        timestamp: (now | todate)
      }
    ]
  }')

RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" -d "$PAYLOAD" "$WEBHOOK_URL")

if [ "$RESPONSE" = "204" ] || [ "$RESPONSE" = "200" ]; then
    echo "✔ Release announcement for ${VERSION} broadcasted successfully to Discord (HTTP $RESPONSE)!"
else
    echo "✖ Failed to send announcement (HTTP $RESPONSE). Please check your webhook URL."
    exit 1
fi
