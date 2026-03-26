# LGPL-3.0 Compliance Guide for MARA

**License**: GNU Lesser General Public License v3.0

---

## What You Can Do

✅ **Sell MARA** — Commercial use is permitted  
✅ **Sell Pi devices with MARA** — Hardware + software sales are permitted  
✅ **Modify MARA** — You can change the code  
✅ **Modify NautilusTrader** — You can modify the trading engine  
✅ **Keep modifications private** — You don't have to publish your changes  
✅ **Use for trading** — No restrictions on profit or trading volume  
✅ **Distribute with custom cases** — Hardware resale is permitted  

---

## What You Must Do

### 1. Include License Files

When you distribute MARA (via GitHub, hardware, or website), include:

```
MARA/
├── LICENSE                    ← LGPL-3.0 full text
├── NOTICE.txt                 ← This file (all attributions)
└── README.md                  ← Mentions LGPL-3.0
```

### 2. Provide Source Code Access

Users have the right to access the source code of:
- MARA hypervisor and workers
- Any modifications you made to NautilusTrader
- All LGPL-3.0 components

**How to provide:**
- Host on GitHub (public repository) — easiest
- Provide download link on your product website
- Send source code upon customer request

### 3. Document Modifications

If you modify NautilusTrader or other LGPL-3.0 code:
- Keep version control history
- Document what you changed and why
- Include timestamps

### 4. Include Build Instructions

If you distribute compiled Docker images or binaries:
- Document how to rebuild from source
- Include Dockerfile references
- Provide requirements.txt

### 5. Use Compatible Licenses

Any code you add to MARA must use a license compatible with LGPL-3.0:

| License | Compatible? | Allowed? |
|---------|-------------|----------|
| LGPL-3.0 | ✅ Yes | Yes |
| MIT | ✅ Yes | Yes |
| Apache 2.0 | ✅ Yes | Yes |
| GPL-3.0 | ⚠️ Stricter | Only if you want GPL-3.0 for all |
| Proprietary | ❌ No | No (your code can be proprietary, but must coexist with LGPL) |

### 6. Keep Copyright Headers

If you distribute unmodified code from other projects, keep their copyright headers.

---

## For Hardware Distribution (Raspberry Pi Cases)

### Checklist Before Shipping

- [ ] Pi SD card includes `LICENSE` file
- [ ] Pi SD card includes `NOTICE.txt` file
- [ ] Product documentation mentions LGPL-3.0
- [ ] Product includes GitHub URL or source code access method
- [ ] Build instructions are documented (in case customer wants to rebuild)
- [ ] No proprietary modifications were made to NautilusTrader or LGPL code

### Sample Product Description

```
MARA Autonomous Trading System
• Powered by NautilusTrader (LGPL-3.0)
• Source code available: https://github.com/[your-org]/mara
• Licensed under LGPL-3.0 — See LICENSE file on SD card

This device includes open-source software that you can modify
and redistribute under the same terms.
```

---

## Violations (Don't Do These)

❌ **Remove license headers** from NautilusTrader or other LGPL code  
❌ **Distribute binaries without source** (without providing access)  
❌ **Claim ownership of LGPL-3.0 code** (Nautech Systems owns NautilusTrader)  
❌ **Relicense to proprietary** (you can't change LGPL-3.0 to closed-source)  
❌ **Hide modifications** (document what you changed)  

---

## FAQs

### Q: Can I sell MARA as a product?
**A:** Yes. LGPL-3.0 permits commercial use and sale.

### Q: Can I charge for support/updates?
**A:** Yes. You can charge for service, support, hosting, or updates.

### Q: Can I keep my modifications private?
**A:** Yes. Your modifications don't need to be published. Only if you distribute them must you provide source.

### Q: Can I add proprietary code on top?
**A:** Yes. Your proprietary code can coexist with LGPL-3.0 code, as long as the LGPL components remain under LGPL-3.0.

### Q: What if a customer modifies MARA?
**A:** They have the right to. They can modify NautilusTrader or MARA. They don't have to share it with you, but if they distribute it, they must share it with their customers.

### Q: Can I relicense to Apache 2.0?
**A:** No. You can't change NautilusTrader's license (that's Nautech's decision). But your new code can be Apache 2.0 (as long as LGPL-3.0 components remain LGPL-3.0).

### Q: What if I replace NautilusTrader later?
**A:** If you use a different trading engine (your own or licensed), you can relicense MARA to whatever you want (Apache 2.0, proprietary, etc.). NautilusTrader will no longer be a dependency.

---

## Resources

- **LGPL-3.0 Full Text**: https://www.gnu.org/licenses/lgpl-3.0.txt
- **GPL/LGPL FAQ**: https://www.gnu.org/licenses/gpl-faq.html
- **SPDX License List**: https://spdx.org/licenses/LGPL-3.0.html

---

## Summary

**LGPL-3.0 is permissive for your hardware+software business model.**

You can:
1. Sell Pi devices with MARA preloaded
2. Charge for hardware, support, customization
3. Keep your business logic proprietary
4. Modify MARA and NautilusTrader for your needs

You just need to:
1. Include LICENSE and NOTICE files
2. Make source code available to customers
3. Document any modifications you make
4. Keep LGPL-3.0 components under LGPL-3.0

This is a standard model used by companies like:
- **Canonical** (Ubuntu + hardware)
- **Redhat** (Linux distributions)
- **Arduino** (open hardware + software)

You're in good company. Ship with confidence.

---

**Last Updated**: 2026-03-26
