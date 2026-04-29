# Tachyon Transcripts — Legal Notice

**This document is not legal advice.** It is a plain-language summary of some of the laws and policies that may apply when you record a conversation, written to help you ask the right questions. If you are unsure whether a specific recording is legal, consult a lawyer in your jurisdiction before recording.

The developer of Tachyon Transcripts provides the software "as is", accepts no liability for how it is used, and makes no warranty of any kind. See [LICENSE](../LICENSE).

## The short version

Recording other people without their knowledge or consent can be illegal, a breach of contract, or both, depending on where you are, what kind of conversation it is, and who the parties are. Tachyon Transcripts does not know any of that — only you do. The app will not let you start a recording until you have acknowledged this notice, but acknowledging it is not a substitute for actually understanding the rules.

If in doubt, do not record.

## United States

Federal wiretap law (18 U.S.C. § 2511) applies a **one-party-consent** rule: recording a conversation you are a party to is generally lawful under federal law. A majority of states follow this rule.

However, **eleven states require all-party consent** for many kinds of recordings — meaning every participant must consent, not just you:

- California (Cal. Penal Code § 632)
- Connecticut
- Florida (Fla. Stat. § 934.03)
- Illinois (720 ILCS 5/14-2)
- Maryland
- Massachusetts (Mass. Gen. Laws ch. 272, § 99)
- Michigan
- Montana
- Nevada
- New Hampshire
- Pennsylvania (18 Pa.C.S. § 5703)
- Washington (RCW 9.73.030)

A few of these have nuances — Connecticut's all-party requirement is civil, not criminal; Nevada's rule depends on whether the conversation is "private"; Michigan's case law has evolved. When the participants are in different states, the strictest applicable law typically governs.

Even in one-party-consent states, recording a conversation you are **not** a party to is generally illegal.

## European Union and United Kingdom

The General Data Protection Regulation (GDPR) applies to recordings that contain identifiable human voices — such recordings are personal data. The UK has a parallel regime under the Data Protection Act 2018 and UK GDPR.

You generally need a lawful basis to process personal data. For recordings of meetings, that is typically informed consent from each participant, though a legitimate-interest basis is sometimes possible if you can demonstrate it in a data protection impact assessment.

You must also tell participants that the recording is happening, why, how long you will keep it, and what their rights are (access, deletion, portability). This is true even if the conversation is internal to a company.

Individual EU member states have additional rules — France's Article 226-1 of the Penal Code criminalises certain recordings of private conversations; Germany's § 201 StGB has similar provisions.

## Canada

Section 184 of the Criminal Code generally follows a one-party consent rule for audio interceptions. PIPEDA applies if the recording is in the course of commercial activity. Quebec (Act Respecting the Protection of Personal Information in the Private Sector) and British Columbia / Alberta (PIPA) have their own privacy regimes.

## Australia

Rules vary significantly by state. Queensland is one-party consent; New South Wales, Victoria, the ACT, Tasmania, and South Australia require all-party consent for most private conversations; Western Australia and the Northern Territory have their own regimes. Federal law (Surveillance Devices Act 2004) applies in some circumstances.

## Workplace considerations (everywhere)

Even when local law would permit a recording, your **employer's policies** may prohibit it. Recording work meetings without permission can be grounds for dismissal and may violate:

- Your employment contract or company handbook.
- Collective-bargaining agreements in unionised workplaces.
- Confidentiality or NDA obligations to clients.
- Occupational privacy standards for healthcare, legal, financial, and other regulated industries.

If your meeting involves:
- Minors
- Patients (HIPAA in the US, equivalent elsewhere)
- Legal clients (attorney-client privilege)
- Union representation
- Children's services, social workers, or protected classes

…the rules are almost always stricter, and the consequences of getting it wrong are almost always worse.

## Meeting-platform terms of service

Zoom, Microsoft Teams, Google Meet, Slack Huddles, Discord, and WebEx all have their own rules about recording, baked into their Terms of Service. Most of them require a visible recording notice be shown to every participant. Using Tachyon Transcripts to record *without* triggering the platform's notice mechanism may violate the ToS even in jurisdictions where the underlying recording would otherwise be legal.

This is a contract issue, not a criminal one — but it can get your account suspended and (in business contexts) expose you or your employer to liability.

## Privileged / confidential relationships

Attorney-client, doctor-patient, clergy-penitent, journalist-source, and similar relationships are governed by much stricter rules than general conversations. In most jurisdictions, recording such a conversation without every party's explicit, informed consent is a serious offence regardless of the general consent rules. Do not do it.

## What the app does to help

Tachyon Transcripts does three things to keep the legal question visible rather than hidden:

1. The installer displays this notice on its **InfoBefore** page during install.
2. The **first-run wizard** shows an abbreviated version of this notice on its own page with a required checkbox. You cannot proceed past that page without ticking it.
3. The **recording gate**: the tray's "Start Recording" menu item will refuse to start a recording if the consent flag is not set, and will re-open the wizard if you try.

None of this makes you legally compliant. It only ensures you cannot claim you were never warned.

## Best-practice checklist

Before you hit record:

- [ ] I am party to this conversation (or I have every non-party participant's consent).
- [ ] I know which jurisdictions the participants are physically in, and I have checked the strictest applicable rule.
- [ ] If my jurisdiction or anyone else's requires all-party consent, I have obtained and documented that consent.
- [ ] Any employer, platform, or contract rules have been satisfied.
- [ ] No participant is in a privileged relationship with me that would be violated by a recording.
- [ ] I have a retention plan (how long I will keep it, where, who can access it, and when I will delete it).

If you cannot tick every box, do not record.

## Reporting a concern

If you believe this software is being used unlawfully against you, please contact the developer at `Pyrodevstudio@gmail.com`. The developer cannot reveal who is running the software — Tachyon Transcripts is 100% local and does not send anything to any server — but if the matter is serious, law enforcement in the relevant jurisdiction may be able to help.

---

*Last updated: 2026-04-20.*
