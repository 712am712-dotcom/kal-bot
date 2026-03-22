import httpx, datetime, sys

WEBHOOK = "https://discord.com/api/webhooks/1484745539114504252/-UmVVdSe4IvnoMoxUloPRiTEEsHTia_nPUMiuqoRG1eoFevM5JShpRdeVvMPnK8NL29G"

payload = {
    "embeds": [{
        "title": "⚡ Kal Online — Systems Nominal",
        "description": "Webhook confirmed. I'm connected and ready to trade.\n\n_All systems green. Research scan in progress._",
        "color": 0x3498DB,
        "fields": [
            {"name": "Status",     "value": "✅ Connected",           "inline": True},
            {"name": "Mode",       "value": "Research (Demo)",         "inline": True},
            {"name": "Checked At", "value": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), "inline": True},
        ],
        "footer": {"text": "Kal • Connection test"},
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }]
}

resp = httpx.post(WEBHOOK, json=payload, timeout=10)
print(f"Status: {resp.status_code}")
if resp.status_code in (200, 204):
    print("✅ Message delivered to Discord successfully.")
else:
    print(f"❌ Failed: {resp.text}")
    sys.exit(1)
