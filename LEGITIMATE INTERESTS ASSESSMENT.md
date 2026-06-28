# LEGITIMATE INTERESTS ASSESSMENT (LIA)

**Document type:** Internal compliance record
**Controller:** GrailSweep (operated by Craig Buckley, sole trader, United Kingdom)
**Date completed:** 02.05.2026
**Date of next review:** 02.05.2027
**Document version:** 1.0

---

## 1. Processing activity covered by this LIA

This assessment covers two related processing activities that together enable abuse prevention on the GrailSweep free tier:

**(a) Server-side device fingerprinting**
A fingerprint hash is computed from each request reaching the `/match` endpoint. The fingerprint is the MD5 hash of: the User-Agent header, the Accept-Language header, and the first two octets of the requesting IP address (e.g. "192.168" rather than the full IP). The full IP address is not retained.

**(b) Storage of monthly scan counters**
A counter is incremented each time a free-tier user completes a successful card identification scan. Counters are keyed by both the device fingerprint described above and a separate randomly generated device identifier stored in the user's browser. Counters reset on the first day of each calendar month UTC.

These activities are technically distinct but functionally inseparable: the fingerprint and counter together enforce the published 150-scans-per-month free tier limit. Neither is meaningful without the other.

---

## 2. Purpose test — is the interest legitimate?

**Stated interest:** Preventing abuse of the GrailSweep free tier, where each card scan incurs real per-call infrastructure cost (GPU compute on Modal and per-call Google Cloud Vision OCR fees), at approximately £0.02 per scan.

**Why this is legitimate:**

- The free tier is offered as a goodwill product trial, funded entirely from Pro subscription revenue.
- Without enforcement, a single user can clear browser data and obtain unlimited free scans, which is both contractually prohibited (Terms of Service §8) and economically unsustainable.
- Abuse prevention is a recognised legitimate interest under UK GDPR. ICO guidance specifically lists "preventing fraud" and "ensuring the security of your systems" as established legitimate-interest examples.
- This is also a commercially necessary interest: without enforcement, the financial viability of the service is at risk, which would harm all users including those acting in good faith.

**Conclusion:** The interest is legitimate, lawful, and clearly defined.

---

## 3. Necessity test — is the processing necessary?

Could we achieve the same purpose by less intrusive means? The alternatives considered:

| Alternative | Effective? | Less intrusive? | Why rejected |
|---|---|---|---|
| Client-side counter only (localStorage) | No | Yes | Trivially bypassed by clearing browser data; provides no actual enforcement. This was the prior state of the system. |
| IP address tracking only | Partially | No | Requires retaining the full IP address, which is more intrusive than truncating to /16. Also unreliable on mobile networks where users share carrier-NAT IPs. |
| Mandatory account creation (email + password) | Yes | No | Requires collecting and processing more personal data (email, possibly name) for every free-tier user, including those who only scan once. Significantly more intrusive. |
| Browser fingerprinting libraries (e.g. FingerprintJS, ThumbmarkJS) | Yes | No | Collects substantially more data points (canvas rendering, WebGL fingerprint, font lists, hardware specs). Designed to identify users across sessions in ways our scheme is not. Materially more intrusive. |
| No enforcement | No | N/A | Abuse continues unchecked; commercial viability at risk. |

The chosen approach uses the **minimum data needed** to achieve the purpose:
- IP address is truncated to the first two octets, sufficient for crude geographic correlation but not individual identification.
- The fingerprint is a one-way MD5 hash; the original inputs are not retained alongside the hash.
- The browser device identifier is a UUID generated client-side with no relationship to the user's real identity.
- No biometric, behavioural, or hardware-fingerprinting data is collected.

**Conclusion:** The processing is necessary; less intrusive means cannot achieve the same purpose.

---

## 4. Balancing test — does the interest override individual rights?

### 4.1 Nature of the data

The data is:
- **Pseudonymous** rather than anonymous (it can be linked to a specific browser/device by reproducing the same hash inputs from a future request).
- **Not directly identifying** — the data does not include name, email, address, payment details, or any government identifier.
- **Not sensitive** — no special-category data (health, ethnicity, sexuality, etc.) is involved.
- **Minimal** — limited to one hash and one UUID per user, plus integer scan counts.
- **Not enriched** — never combined with data from other sources, sold, or used for any purpose other than the stated one.

### 4.2 Reasonable expectations

Would a typical user reasonably expect this processing? Yes:
- The free tier is publicly described as having a per-month limit. Enforcing that limit is a foreseeable corollary.
- Industry-standard freemium services (Spotify, Netflix, ChatGPT, etc.) all operate similar abuse-prevention mechanisms; users are accustomed to them.
- The processing is fully disclosed in the published Privacy Policy (§§2, 4, 6, 10) and Terms of Service (§§4, 8).

### 4.3 Impact on the individual

What is the impact on a user whose data is processed?
- **No financial impact.** The data is not used in pricing decisions, credit assessments, or any commercial profiling.
- **No marketing impact.** The data is not used to send any communication, target advertising, or build behavioural profiles.
- **No third-party impact.** The data is not shared with any third party for their own purposes.
- **Minimal disclosure impact.** A user can exercise their UK GDPR rights (access, deletion, objection) at any time by contacting support, with no friction beyond email.
- **The only practical consequence** is that the user is correctly limited to 150 free scans per calendar month, as advertised.

### 4.4 Safeguards

The following safeguards reduce intrusiveness:
- IP truncation to /16 (first two octets only).
- One-way hashing (MD5) of fingerprint inputs.
- No retention of raw fingerprint inputs alongside the hash.
- Storage on a managed cloud service (Modal) with appropriate security controls.
- Periodic review (annual minimum) to confirm continued necessity and proportionality.
- Clear disclosure in the Privacy Policy with a stated lawful basis.
- Easy user-facing path to upgrade and remove the limit (which removes the need to track them at all under the abuse-prevention purpose).

### 4.5 Children

The service is not directed at children under 13 (Terms of Service §10, Privacy Policy §8). Any processing of a child's data would not change the legitimate-interest analysis materially, but would attract additional scrutiny under UK GDPR Article 8. As the data collected is minimal and non-marketing, the risk to children specifically is comparable to the risk to adults.

### 4.6 Conclusion

The interest in abuse prevention is **proportionate** to the impact on the individual, given:
- The minimal nature and quantity of data collected.
- The absence of any marketing, profiling, or financial use.
- The clear disclosure and the user's ability to exercise their rights.
- The reasonable expectations of users in this market.

The legitimate interest **outweighs** the impact on the individual's rights and freedoms. The processing is therefore lawful under UK GDPR Article 6(1)(f).

---

## 5. Outcome

The processing described in §1 of this assessment is approved on the basis of legitimate interest. It will be reviewed annually, or sooner if any of the following triggers occur:

- A material change in the data collected (e.g. switch to client-side fingerprinting libraries that collect richer data).
- A material change in retention period or scope.
- A user complaint or ICO query relating to this processing.
- A change in law or regulatory guidance affecting the analysis.

---

## 6. Review log

| Date | Reviewer | Outcome | Notes |
|---|---|---|---|
| [DATE] | Craig Buckley | Approved at v1.0 | Initial assessment created at time of Phase 2 deployment. |

---

## 7. Document control

- **Storage location:** [e.g. Google Drive: GrailSweep/Compliance/LIA-Fingerprinting-v1.0.md]
- **Disclosure rules:** Internal only. Not published. May be disclosed to: ICO on request; legal counsel; data subject in a SAR if directly relevant.
- **Retention:** Retained for as long as the underlying processing continues, plus 6 years after cessation (matching the UK statute of limitations for civil claims).