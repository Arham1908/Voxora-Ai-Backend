"""
sip_config.py — environment-driven SIP configuration.

Set SIP_MODE in .env to select the active mode:

  multinet  →  Python registers directly to Multinet
               uses MultinetRegistrar (IP-auth + Digest fallback)

  asterisk  →  Python registers to local Asterisk via pyVoIP
               Asterisk handles Multinet registration (pjsip.conf)
               Python's own IP is auto-detected at startup — works at
               flat, office, Docker without changing this file

  local     →  MicroSIP softphone registers directly to Python
               no Multinet, no Asterisk — pure local dev testing
"""

import os

# ── Mode ─────────────────────────────────────────────────────────────
SIP_MODE = os.environ.get("SIP_MODE", "local")
# Values: "multinet" | "asterisk" | "local"

# ── Python's own SIP/RTP bind settings ───────────────────────────────
# SIP_BIND_IP: leave blank / "0.0.0.0" to auto-detect LAN IP
# SIP_BIND_PORT: port Python listens on for incoming SIP (INVITEs)
#   multinet mode → Multinet sends INVITEs here directly
#   asterisk mode → Asterisk sends INVITEs here after receiving from Multinet
#   local mode    → MicroSIP sends INVITEs here
SIP_BIND_IP      = os.environ.get("SIP_BIND_IP", "0.0.0.0")
SIP_BIND_PORT    = int(os.environ.get("SIP_BIND_PORT", 5060))
SIP_RTP_PORT_LOW = int(os.environ.get("SIP_RTP_PORT_LOW",  10000))
SIP_RTP_PORT_HIGH= int(os.environ.get("SIP_RTP_PORT_HIGH", 20000))

# ── Multinet trunk settings (multinet mode) ───────────────────────────
# Python registers directly to Multinet.
# Also read by Asterisk pjsip.conf in asterisk mode (set same values).
SIP_SERVER      = os.environ.get("SIP_SERVER",      "multinet.bt")
SIP_SERVER_PORT = int(os.environ.get("SIP_SERVER_PORT", 5083))
SIP_USERNAME    = os.environ.get("SIP_USERNAME",    "")   # e.g. 924232560022lbo
SIP_PASSWORD    = os.environ.get("SIP_PASSWORD",    "")   # e.g. Cisco@123

# SIP_PUBLIC_IP: only needed at Multinet office or behind NAT
# Leave blank in Docker / most setups — auto-detected from local_ip
SIP_PUBLIC_IP   = os.environ.get("SIP_PUBLIC_IP",   "")

# ── Asterisk bridge settings (asterisk mode) ─────────────────────────
# ASTERISK_HOST: IP or hostname of the Asterisk machine
#   Docker:       "asterisk"       (Docker service name, resolves automatically)
#   Flat LAN:     "192.168.x.x"   (Asterisk machine's LAN IP at home)
#   Office LAN:   "10.x.x.x"      (Asterisk machine's LAN IP at office)
#   Multinet HQ:  switch SIP_MODE=multinet instead — no Asterisk needed there
#
# ASTERISK_PORT: Asterisk's SIP port (default 5060, standard pjsip)
# ASTERISK_USERNAME / ASTERISK_PASSWORD: matches [pyvoip-auth] in pjsip.conf
# SIP_BIND_PORT in asterisk mode: Python listens here for Asterisk's INVITEs
#   → must match contact=sip:{username}@{ip}:{SIP_BIND_PORT} that pyVoIP registers
#   → default 5061 to avoid clash with Asterisk's own 5060
ASTERISK_HOST     = os.environ.get("ASTERISK_HOST",     "")
ASTERISK_PORT     = int(os.environ.get("ASTERISK_PORT", 5060))
ASTERISK_USERNAME = os.environ.get("ASTERISK_USERNAME", "100")
ASTERISK_PASSWORD = os.environ.get("ASTERISK_PASSWORD", "")
SIP_AGENT_ID=os.environ.get("SIP_AGENT_ID",'')
SIP_LANGUAGE=os.environ.get("os.environ.get",'')
SIP_VOICE=os.environ.get("SIP_VOICE",'')
# ── Local / MicroSIP test settings (local mode) ──────────────────────
# MicroSIP registers with these credentials to Python's RawSIPServer
SIP_TEST_USERNAME = os.environ.get("SIP_TEST_USERNAME", "100")
SIP_TEST_PASSWORD = os.environ.get("SIP_TEST_PASSWORD", "test1234")