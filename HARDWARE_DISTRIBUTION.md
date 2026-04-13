# MARA Hardware Distribution Guide

For selling Raspberry Pi devices with ARKA preloaded (with or without custom cases).

---

## What To Include With Every Device

### Physical/Digital Package

```
MARA Autonomous Trading System Box
├── Raspberry Pi 5 (8GB RAM) with custom case
├── USB-C power supply (45W recommended)
├── microSD card (256GB, MARA preloaded)
├── Quick Start Guide (printed or digital)
└── License & Documentation (digital or printed)
```

### On the microSD Card

```
/boot/
├── MARA system files
└── [standard Pi boot files]

/root/mara/
├── LICENSE                    ← LGPL-3.0 license
├── NOTICE.txt                 ← Third-party attributions
├── LGPL3_COMPLIANCE.md        ← Compliance guide
├── README.md                  ← Full documentation
└── [MARA source code]
```

### In the Box (Physical)

Include one of these:
- Printed copy of LICENSE + NOTICE.txt
- QR code linking to online documentation
- Card with GitHub URL and setup instructions

---

## Quick Start Guide Template

```
╔═══════════════════════════════════════════════════════════╗
║  MARA Autonomous Trading System                           ║
║  Quick Start Guide                                        ║
╚═══════════════════════════════════════════════════════════╝

1. POWER UP
   • Connect 45W USB-C power supply
   • Wait 2 minutes for system boot
   • LED indicator: GREEN = ready

2. NETWORK
   • Connect Ethernet (recommended) OR
   • Connect via WiFi (SSID: MARA-[serial], password: on sticker)

3. ACCESS MARA
   • Browser: http://mara.local:8000
   • SSH: ssh -l pi 192.168.1.XXX (IP on boot screen)
   • Default password: raspberry

4. CONFIGURATION
   • Run: mara configure
   • Set exchange API keys (OKX only — Binance/Bybit geo-blocked)
   • Set ACLED credentials for conflict index
   • Set target capital: $200 (paper trading default)

5. START TRADING
   • Run: mara start
   • Monitor at http://mara.local:8000/status
   • View logs: mara logs

6. MODIFY (OPTIONAL)
   • Edit config files in /root/mara/config/
   • View source code: /root/mara/
   • Rebuild: ./scripts/build_pi.sh
   • Restart: docker compose restart

═══════════════════════════════════════════════════════════

LEGAL
This device includes open-source software distributed under
the GNU Lesser General Public License v3.0 (LGPL-3.0).

You have the right to:
✓ Modify the software
✓ View the source code
✓ Redistribute under the same terms

See LICENSE file on the SD card for details.
Source code: https://github.com/shrijitb/mara

═══════════════════════════════════════════════════════════

SUPPORT
• Documentation: https://github.com/shrijitb/mara/wiki
• Issues: https://github.com/shrijitb/mara/issues

═══════════════════════════════════════════════════════════
```

---

## Pre-Shipping Checklist

### Software (on SD card)
- [ ] MARA source code present and complete
- [ ] LICENSE file included
- [ ] NOTICE.txt file included
- [ ] LGPL3_COMPLIANCE.md file included
- [ ] README.md with documentation
- [ ] config/ directory with example configs
- [ ] scripts/ with build/deploy scripts
- [ ] .env.example with required keys
- [ ] No API keys or secrets hardcoded
- [ ] Git history preserved (for documentation)

### Hardware
- [ ] Raspberry Pi 5 (8GB) tested and booting
- [ ] Custom case fits Pi correctly
- [ ] Power supply included (45W USB-C)
- [ ] microSD card formatted and imaged
- [ ] All containers tested: hypervisor, nautilus, polymarket, autohedge, arbitrader
- [ ] Network connectivity working (Ethernet + WiFi)
- [ ] Quick Start Guide printed or included digitally

### Documentation
- [ ] Quick Start Guide in box or on SD card
- [ ] License notice included
- [ ] Support contact information provided
- [ ] GitHub repository link provided
- [ ] Warranty/support terms clarified

### Compliance
- [ ] No proprietary modifications to NautilusTrader without documenting
- [ ] All LGPL-3.0 obligations documented
- [ ] Source code availability method clear (GitHub)
- [ ] Build instructions provided
- [ ] Third-party attributions complete

---

## Customer Communication Template

### Product Page

```
MARA Autonomous Trading System — Raspberry Pi Edition

An open-source, multi-agent trading system for crypto, commodities,
and prediction markets. Powered by NautilusTrader.

Features:
• Multi-agent regime-aware capital allocation
• Swing trading, market-making, arbitrage
• LGPL-3.0 licensed — fully open-source
• Self-hosted on Raspberry Pi
• Paper trading + live execution modes

Hardware Included:
• Raspberry Pi 5 (8GB RAM)
• Custom MARA case with cooling
• 45W USB-C power supply
• 256GB microSD card (MARA preloaded)
• Quick start guide

Legal Notice:
This device includes open-source software licensed under the GNU
Lesser General Public License v3.0. You have the right to modify,
audit, and redistribute the software under the same terms.

Source code available at: https://github.com/[your-org]/mara
License file included on SD card.
```

---

## After-Sales Support

### What to Provide

1. **Email support** for setup issues
2. **Discord/Slack community** for questions
3. **GitHub issues** for bug reports
4. **Wiki/docs** for customization guides
5. **Build scripts** for rebasing (if they modify code)

### What NOT to Do

- ❌ Don't remove LICENSE or NOTICE files
- ❌ Don't modify source code and claim it's original
- ❌ Don't prevent customers from modifying code
- ❌ Don't require proprietary license for common customizations

---

## Scaling Notes

### For 10s of Units
- Burn microSD cards manually
- Use SD card imager (balena Etcher)
- Assemble cases by hand
- Test each device individually

### For 100s of Units
- Use SD card burning service (SolidRun, etc.)
- Automate imaging with Ansible/Terraform
- Batch test with Bash scripts
- Use manufacturing partner for case assembly

### For 1000s+ Units
- Contract with Pi reseller (CanaKit, Pi Supply, etc.)
- Have them preload custom image
- They handle case assembly + shipping
- You provide: image build, documentation, support

---

## Licensing Guarantee

When you ship a device with ARKA:

1. **You are in full compliance** with LGPL-3.0 if:
   - [ ] LICENSE file is included
   - [ ] NOTICE.txt is included
   - [ ] Source code is accessible (GitHub)
   - [ ] Build instructions are provided
   - [ ] Modifications to LGPL code are documented

2. **You can commercially sell** because LGPL-3.0 permits:
   - Commercial distribution
   - Hardware sales
   - Service charges
   - Support tiers
   - Software updates

3. **You do NOT need permission** to:
   - Modify NautilusTrader
   - Customize ARKA
   - Sell devices
   - Keep modifications private
   - Charge for hardware

---

## Example Build for First 10 Units

```bash
# 1. Clone MARA repo
git clone https://github.com/shrijitb/mara
cd mara

# 2. Build ARM64 Docker images for Pi
docker buildx build --platform linux/arm64 -t mara:latest .

# 3. Create Pi SD card image
# (Use Raspberry Pi Imager or custom script)

# 4. Copy MARA to image
# In /root/mara/:
#   - Copy entire repo (including .git for history)
#   - Ensure LICENSE, NOTICE.txt present
#   - Create .env with defaults

# 5. Add Quick Start Guide
# Copy to /root/QUICK_START.txt

# 6. Test
# - Boot Pi
# - Run docker compose up -d
# - Wait 60s for hypervisor
# - curl http://localhost:8000/health

# 7. Burn to microSD cards
balena-etcher-cli mara.img.zip --drive /dev/sdX --yes

# 8. Assemble & ship
```

---

## Summary

**LGPL-3.0 is perfect for hardware+software sales:**

✅ You can sell Pis with MARA preloaded  
✅ You can charge for the hardware and service  
✅ You can modify MARA for your needs  
✅ You keep your modifications private  
✅ You just need to provide source access  

**Just include:**
- LICENSE file
- NOTICE.txt file
- GitHub link
- Quick Start Guide

**Ship with confidence.**

---

**Last Updated**: 2026-03-26
