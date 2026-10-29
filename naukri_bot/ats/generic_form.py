"""Fill any company's job-application form by its labels (Zoho Recruit, Odoo, Greenhouse, Lever, Workable, custom
WordPress forms...). Opens the form (clicks "Apply" / "I'm interested" when the form is not on the page), fills
contact details, links, CTC, notice period and experience from the profile, uploads the resume tailored to this job,
answers selects/radios through the same rules as Naukri, ticks consent boxes, and stops before Submit unless asked.

    python -m naukri_bot company-apply --file inputs/company-list.txt [--submit]
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
import time
from dataclasses import dataclass, field

from ..rules import rule_answer, with_unit

log = logging.getLogger(__name__)

LABEL_JS = r"""e => {
  const ok = t => t && /[A-Za-z]{2}/.test(t) && t.trim().length > 1;
  const clean = t => t.replace(/\s+/g, ' ').trim().slice(0, 160);
  const byFor = e.id && document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
  if (byFor && ok(byFor.innerText)) return clean(byFor.innerText);
  const aria = e.getAttribute('aria-label'); if (ok(aria)) return clean(aria);
  const lb = e.getAttribute('aria-labelledby');
  if (lb) { const t = lb.split(' ').map(i => (document.getElementById(i) || {}).innerText || '').join(' '); if (ok(t)) return clean(t); }
  const wrap = e.closest('label'); if (wrap && ok(wrap.innerText)) return clean(wrap.innerText);
  // climb: the nearest ancestor that holds a label-like element with real words (Zoho puts it ~6 levels up).
  // Within that ancestor take the label just BEFORE the field: on multi-field rows (PyjamaHR) the first label in
  // a wide ancestor belongs to another field ("Contact Number" for First Name). Past two levels a descriptive
  // placeholder ("First Name", "Notice Period") is the better witness than a far-away label.
  const ph = e.placeholder;
  const before = l => l.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING;
  // a label with another visible field between it and `e` belongs to that field ("Phone" is not the City's label)
  const owned = (l, root) => [...root.querySelectorAll('input:not([type=hidden]), select, textarea')].some(c =>
    c !== e && (l.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING) && (c.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING));
  // last resort: a descriptive name/id ("Current_CTC_in_lakhs" -> "Current CTC in lakhs")
  const nm = (e.name || e.id || '').replace(/[_-]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/\b(mui|input|react select)\b.*$/i, '').trim();
  const named = /[A-Za-z]{3,}\s+[A-Za-z]{2,}/.test(nm) ? nm : '';
  let p = e.parentElement;
  for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
    if (i === 2 && ok(ph) && !/^(select|search|type|enter here)/i.test(ph)) return clean(ph);
    const cands = [...p.querySelectorAll('label, legend, [class*=label], [class*=Label]')].filter(l => !l.contains(e) && ok(l.innerText));
    if (!cands.length) continue;
    const prev = cands.filter(l => before(l) && !owned(l, p));
    if (prev.length && prev[prev.length - 1].innerText.trim().length > 3) return clean(prev[prev.length - 1].innerText);
    if (!cands.some(before)) return clean(cands[0].innerText);   // label after the field (radio/checkbox text)
    break;                                                        // nearby labels all belong to other fields
  }
  if (ok(ph) && !/^(select|search)/i.test(ph)) return clean(ph);
  return named;
}"""

# Statements that are (or may be) legal claims about the candidate: never auto-ticked or auto-confirmed.
LEGAL_CLAIM = re.compile(
    r"work(ing)? authori[sz]ation|authori[sz]ed (\(or eligible\) |or eligible )?to work|eligible to work|right to work|work permit|"
    r"(legally )?(permitted|allowed|entitled) to work|\bcitizen(ship)?s?\b|nationality|sponsor|\bvisa\b|immigration|"
    r"criminal|convict|felon|offen[cs]e|arrest|background (check|verification|screening|investigation)|"
    r"relocat|\bbond\b|service agreement|deposit|medical|\bhealth|disab|\bdrugs?\b|substance|alcohol|"
    r"non-?compete|restrictive covenant|non-?solicit|salary|compensation|\bctc\b|\bpay\b|remuneration|stipend|"
    r"resid(e|ent|ency|ence)\b|green card|\b(us|u\.s\.|uk|u\.k\.|eu|united states|united kingdom|american|british|canadian)\s+(person|citizen|national|resident)|"
    r"export control|true,? (complete|correct|and accurate)|accurate and complete|certify|declare|under penalty|perjury|"
    r"terminat|dismiss|disciplinary|conflicts? of interest|related to (an?|any) (employee|official)|government official", re.I)
# The only acknowledgements the bot may tick/confirm on the candidate's behalf: privacy, terms of use, data processing/retention.
CONSENT_OK = re.compile(
    r"privacy|terms (of (use|service)|and conditions|& conditions)|\bterms\b|data (processing|protection|retention|privacy)|"
    r"process(ing|es|ed)? (of )?(my |your )?(personal )?(data|information)|personal (data|information)|retain|retention|cookie|gdpr|"
    r"storage and handling|store (and|&) (handle|process)|storage of (my |your )?(data|information)|"
    r"(may|to|can) (store|keep|hold) my (application|cv|resume|data|details)|"            # Cyepro (21 Oct 2026)
    r"(point of |cross[- ]border )?data transfer|transfer of (my |your )?(personal )?(data|information)|"   # Lynx (13 Oct 2026)
    # "how my information will be processed / used" (Visa Workday, 14 Oct 2026)
    r"how (my|your) (personal )?(data|information) (will be|is|are) (processed|used|handled|stored|collected)|"
    # "consent to X collecting, storing, and processing my responses to the demographic data surveys" (Backbase, 15 Oct 2026)
    r"consent to .{0,40}(collect|stor|process)\w*.{0,60}(responses|answers|survey|demographic)", re.I)
# acknowledgement-style wording (a statement to tick/confirm rather than a fact to fill in)
ACK_WORDING = re.compile(
    r"\bi (agree|consent|acknowledge|confirm|accept|am allowing|allow|authori[sz]e|certify|declare|understand|attest)\b|please confirm|"
    r"by (checking|clicking|selecting|submitting|ticking)|have (read|reviewed)|\bconsent\b|privacy|terms (and|&|of)|confirm the statement", re.I)
# questions about the right to work / sponsorship: only answerable for a job in the candidate's authorized country
WORK_AUTH_Q = re.compile(
    r"(require|need).{0,60}sponsor|visa sponsorship|sponsor.{0,20}visa|authori[sz]ed (\(or eligible\) |or eligible )?to work|"
    r"eligible to work|right to work|work permit|(legally )?(permitted|entitled) to work", re.I)
# ...except these, which ask about the candidate (which countries they may work in, and on what basis)
CANDIDATE_COUNTRY_Q = re.compile(r"countr(y|ies).{0,40}(right to work|authori[sz]ed|residence|reside)|source of your right to work|"
                                 r"basis of your (right|authori[sz]ation)", re.I)
# 10th / 12th (school-level) education fields: never the university's CGPA, year or name
# "How did you hear about us" in all its wordings ("Where did you hear about us", Detect Technologies 15 Oct 2026)
HEARD_Q = re.compile(r"(how|where) (did|have) you (first )?(hear|learn|find|come across|get to know)|how you heard|^\s*source\b|referral source", re.I)
SCHOOL_LEVEL = re.compile(r"10th|12th|\bclass (x|xii|10|12)\b|\bx(ii)?(th)? (std|standard)\b|high school|(?<!post-)(?<!post )secondary|\bssc\b|\bhsc\b|matric", re.I)
_TWELFTH = re.compile(r"12th|\bclass (xii|12)\b|\bxii\b|\bhsc\b|higher secondary|senior secondary|intermediate", re.I)
_TENTH = re.compile(r"10th|\bclass (x|10)\b|\bssc\b|secondary school certificate|matric", re.I)
# fields only the person can fill: dates of a specific education/employment entry
PER_ENTRY_DATE = re.compile(r"\b(start|end|from|to|joining|leaving) date\b.{0,10}\b(month|year)\b|\b(start|end) (month|year)\b", re.I)

_INDIA = re.compile(
    r"\bindia\b|\b(bengaluru|bangalore|mumbai|bombay|new delhi|delhi|gurugram|gurgaon|noida|greater noida|hyderabad|pune|chennai|"
    r"kolkata|ahmedabad|jaipur|kochi|cochin|chandigarh|mohali|thiruvananthapuram|trivandrum|coimbatore|indore|mysuru|mysore|"
    r"nagpur|lucknow|bhubaneswar|vadodara|visakhapatnam|surat|gandhinagar)\b", re.I)
_ABROAD = re.compile(
    r"(?i:\b(united states|united kingdom|england|scotland|london|manchester|canada|toronto|vancouver|montreal|germany|berlin|munich|"
    r"singapore|australia|sydney|melbourne|ireland|dublin|netherlands|amsterdam|france|paris|spain|madrid|barcelona|poland|warsaw|"
    r"krakow|japan|tokyo|china|shanghai|beijing|hong kong|mexico|brazil|philippines|manila|dubai|abu dhabi|saudi arabia|riyadh|"
    r"israel|tel aviv|switzerland|zurich|sweden|stockholm|new york|san francisco|seattle|boston|chicago|indianapolis|indiana|"
    r"california|texas|atlanta|denver|portugal|lisbon|romania|bucharest|czech|prague|hungary|budapest|vietnam|malaysia|"
    r"kuala lumpur|indonesia|jakarta|south africa|new zealand|united arab emirates)\b)"
    r"|\bUSA\b|(?<![A-Za-z])U\.S\.|\bUK\b|(?<![A-Za-z])U\.K\.|\bUAE\b"
    r"|(?i:\b(remote|location|based|located|in|within|across)\s*[-:,(]?\s*(the\s+)?)US\b|\bUS[- ](?i:based|remote|only)\b")


def consent_only(label: str) -> bool:
    """True when a statement is purely a privacy / terms / data-processing acknowledgement (safe to tick)."""
    return bool(CONSENT_OK.search(label or "")) and not LEGAL_CLAIM.search(label or "")


def job_country_from(text: str, url: str = "") -> str | None:
    """Where the job is, from the page: 'India' (India / an Indian city named, nothing abroad), 'other' (only
    places abroad), 'mixed' (both), or None (no evidence). 'Indianapolis' and 'Indiana' are not India."""
    # the job's own location line sits at the top (title, then "Bangalore, India"); a form further down can list
    # every US state in a dropdown (New Relic, 21 Oct 2026), which must not make an Indian job look "mixed"
    head = (text or "")[:200]
    if _INDIA.search(head) and not _ABROAD.search(head):
        return "India"
    hay = f"{text or ''}\n{re.sub(r'[-_/+.%=?&]+', ' ', url or '')}"
    india, abroad = bool(_INDIA.search(hay)), bool(_ABROAD.search(hay))
    if india and abroad:
        return "mixed"
    return "India" if india else "other" if abroad else None


def cover_letter_pdf(role: str, url: str, profile: dict, out_dir) -> "Path":
    """A short, plain cover letter PDF for upload fields that ask for one. Facts only from the profile."""
    from pathlib import Path
    from urllib.parse import urlparse
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape as esc
    host = urlparse(url or "").netloc.replace("www.", "").split(".")[0] or "your team"
    company = host.replace("-", " ").title()
    role = (role or "the open role").strip()
    name = profile.get("name", "")
    st = ParagraphStyle("b", fontName="Helvetica", fontSize=10.5, leading=15)
    paras = [
        f"Dear Hiring Team at {company},",
        f"I am applying for the {role} position. I am a Python Full Stack AI/ML Software Engineer with 2 years of "
        "experience building production backends, REST APIs and LLM features with Python, FastAPI, React and Node.js.",
        "Recently I built an enterprise RAG pipeline over 1,000+ documents at 85% retrieval accuracy, cut API latency "
        "from 400 ms to under 150 ms for a platform serving 10,000+ users, and built ML inference pipelines with Redis "
        "caching that removed 60% of repeated database queries.",
        f"I can join immediately and would welcome the chance to discuss how I can contribute to {company}.",
        f"Sincerely,<br/>{esc(name)}<br/>+91-{profile.get('phone_national', '')} | {profile.get('email', '')}",
    ]
    out = Path(out_dir) / f"{name.replace(' ', '_')}_Cover_Letter_{host}.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm, topMargin=25 * mm, bottomMargin=20 * mm,
                            title=f"{name} - Cover Letter", author=name)
    story = []
    for t in paras:
        story += [Paragraph(t if t.startswith("Sincerely") else esc(t), st), Spacer(1, 8)]
    doc.build(story)
    return out


def _num(v) -> str:
    """7.5 -> '7.5', 14.0 -> '14'."""
    f = float(v)
    return str(int(f)) if f == int(f) else str(f)


def company_matches(value: str, current: str) -> bool:
    """Is an employment block's company the candidate's current company? (case-insensitive substring either way)"""
    a, b = (value or "").strip().lower(), (current or "").strip().lower()
    return bool(a and b) and (a in b or b in a)


# the company input of the employment block a "currently work here" checkbox belongs to (nearest ancestor holding one)
BLOCK_COMPANY_JS = r"""e => {
  const rx = /compan|employer|organi[sz]ation/i;
  let p = e.parentElement;
  for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
    for (const inp of p.querySelectorAll("input[type='text'], input:not([type])")) {
      const lab = (inp.id && document.querySelector('label[for="' + CSS.escape(inp.id) + '"]')) || null;
      const words = [inp.name, inp.id, inp.placeholder, inp.getAttribute('aria-label'), lab && lab.innerText].join(' ');
      if (rx.test(words)) return (inp.value || '').trim();
    }
  }
  return '';
}"""

CONFIRM_RX = re.compile(r"thank you( for (applying|your application|your interest|submitting))?|thanks for (applying|taking the time)|"
                        r"application (has been )?(received|submitted)|successfully (applied|submitted)|we.ll be in touch", re.I)


def confirmation_seen(before_body: str, after_body: str, before_url: str = "", after_url: str = "",
                      form_gone: bool = False) -> bool:
    """Did Submit work? Only evidence that appeared after the click counts: a confirmation phrase that was not
    on the page before, a new thank-you/success URL, or the form vanishing while new text appeared."""
    from collections import Counter
    count = lambda t: Counter(m.group(0).lower() for m in CONFIRM_RX.finditer(t or ""))    # noqa: E731
    before, after = count(before_body), count(after_body)
    if any(n > before.get(k, 0) for k, n in after.items()):
        return True
    if after_url and after_url != before_url and re.search(r"thank|success|submitted|confirmation", after_url, re.I):
        return True
    if form_gone:
        old = {ln.strip() for ln in (before_body or "").splitlines() if ln.strip()}
        new = [ln.strip() for ln in (after_body or "").splitlines() if len(ln.strip()) >= 15 and ln.strip() not in old
               and not re.search(r"submitting|please wait|loading|processing|uploading|error|required|invalid", ln, re.I)]
        return bool(new)
    return False


@dataclass
class FormResult:
    status: str                       # ready_to_submit | applied | needs_you | no_form | failed
    notes: str = ""
    filled: list[dict] = field(default_factory=list)


# Job-alert sign-ups, talent-community boxes, newsletters and job-search bars sit on the same page as the job
# (Amgen / Barclays Radancy pages, 24-25 Oct 2026: the bot filled "Sign Up for Job Alerts" and skipped APPLY NOW)
SIDE_WIDGET_JS = """e => { let n = e;
  for (let i = 0; i < 7 && n; i++, n = n.parentElement) {
    const body = n.innerText || '';
    if (body.length > 1500) break;                     // past the widget: the page around it says nothing about this field
    const t = ((n.getAttribute && (n.getAttribute('aria-label') || '')) + ' ' + body);
    if (/job alerts?|talent (community|network)|sign ?up for (job )?alerts|newsletter|subscribe|search jobs|radius/i.test(t)) return true;
    if (n.tagName === 'FORM') break;
  }
  return false; }"""


class GenericFormApplier:
    def __init__(self, page, profile: dict, resume_path: str, role: str = "", submit: bool = False, code_file: str | None = None):
        self.pg, self.profile, self.resume = page, profile, os.path.abspath(resume_path)
        self.role, self.submit, self.code_file = role, submit, code_file
        self.result = FormResult("failed")
        self._origin = page                       # the tab this application started in
        self._opened: list = []                   # tabs this application opened (closed when it ends)
        self._country_cache: dict[str, str | None] = {}
        self._start_country: str | None = None

    # ------------------------------------------------------------------ where the job is
    _PAGE_TEXT_JS = r"""() => {
      const skip = "select, option, datalist, [role=listbox], [role=option], lyte-drop-box, lyte-drop-item, script, style, noscript";
      const out = []; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let n = w.nextNode(); n; n = w.nextNode()) {
        const p = n.parentElement; if (!p || p.closest(skip) || !p.getClientRects().length) continue;
        const t = n.textContent.trim(); if (t) out.push(t);
      }
      return out.join('\n'); }"""

    def _page_text(self) -> str:
        """Visible page text, without dropdown option lists (a Country select lists every country)."""
        try:
            return self.pg.evaluate(self._PAGE_TEXT_JS) or ""
        except Exception:
            try:
                return self.pg.inner_text("body")
            except Exception:
                return ""

    def job_country(self) -> str | None:
        """'India' / 'other' / 'mixed' / None for the current page (cached per URL); a page with no evidence
        falls back to what the job page the application started from said."""
        if self.pg is None:
            return None
        try:
            url = self.pg.url or ""
        except Exception:
            return None
        if url not in self._country_cache:
            self._country_cache[url] = job_country_from(self._page_text(), url)
        found = self._country_cache[url]
        return found if found is not None else self._start_country

    def _authorized_here(self) -> bool | None:
        """True when the job is in the candidate's authorized country, else None (unknown: the person answers)."""
        auth = self.profile.get("work_authorized_in")
        auth = [auth] if isinstance(auth, str) else list(auth or [])
        auth = [str(a).strip().lower() for a in auth if a]
        country = self.job_country()
        if not auth or country in (None, "other", "mixed"):
            return None
        return True if country.lower() in auth else None

    def _work_auth_value(self, l: str) -> str | None:
        """Yes/No for right-to-work / sponsorship questions, only for a job in the authorized country."""
        auth = self.profile.get("work_authorized_in")
        auth = [auth] if isinstance(auth, str) else list(auth or [])
        auth_l = [str(a).strip().lower() for a in auth if a]
        sponsor = re.search(r"sponsor", l)
        named = re.search(r"\bto work in ([a-z ]+?)(\?|$|\.|,| without| for)", l)       # "authorized to work in India?"
        if named and named.group(1).strip() not in ("the country", "this country", "the location", "this location",
                                                     "the country where this job is located", "your current location"):
            place = named.group(1).strip()
            ok = place in auth_l
        else:
            ok = self._authorized_here()
        if not ok:
            return None
        return "No" if sponsor else "Yes"

    # ------------------------------------------------------------------ values from the profile
    def _school_short(self) -> str | None:
        """The school's short multi-word name from school_aliases (for searchable lists), else the full name."""
        aliases = [a for a in self.profile.get("school_aliases") or [] if a and " " in a.strip() and not a.isupper()]
        return min(aliases, key=len) if aliases else self.profile.get("school")

    def _school_acronym(self) -> str | None:
        return next((a for a in self.profile.get("school_aliases") or [] if a and a.isupper() and " " not in a.strip()), None)

    def _school_names_rx(self) -> str | None:
        names = [n for n in [self.profile.get("school")] + list(self.profile.get("school_aliases") or []) if n]
        names += [re.sub(r"\s*\(.*?\)", "", n.split(",")[0]).strip() for n in names]
        names = sorted({n for n in names if n}, key=len, reverse=True)
        return "|".join(rf"\b{re.escape(n)}\b" for n in names) if names else None

    def _primary_language(self) -> str | None:
        p = self.profile
        sy = {str(k).lower(): v for k, v in (p.get("skill_years") or {}).items()}
        has_py = "python" in sy or any(str(s).lower() == "python" for s in p.get("skills_primary") or [])
        yrs = sy.get("python") or p.get("relevant_experience_years")
        if not has_py or not yrs:
            return None
        also = [n for k, n in (("typescript", "TypeScript"), ("javascript", "JavaScript")) if k in sy]
        return f"Python (about {_num(yrs)} years in production)" + (f", also {'/'.join(also)}" if also else "")

    def _ctc_text(self, which: str, label: str = "") -> str | None:
        p = self.profile
        v = p.get(f"{which}_ctc_lpa")
        if v in (None, ""):
            return None
        # unit named in the label: rupees -> whole number (7.5 LPA -> 750000), lakhs -> the bare number (7.5).
        # Number-only boxes strip "INR 7.5 LPA" to "75", so never send words there (Veersa, 12 Oct 2026).
        if re.search(r"\(inr\)|in inr|\binr\b|in rupees|\brs\.?\b|₹|per annum in|\(in numbers\)", label, re.I) and not re.search(r"lakh|lac|lpa", label, re.I):
            return str(int(round(float(v) * 100000)))
        if re.search(r"lakh|lac|lpa", label, re.I):
            return _num(v)
        neg = " (negotiable)" if which == "expected" and p.get("ctc_negotiable") else ""
        return f"INR {_num(v)} LPA{neg}"

    def _school_level(self, l: str) -> tuple[bool, str | None]:
        """10th / 12th fields: (handled, value). Only their percentage is known (marks_10th/12th_percent)."""
        if not SCHOOL_LEVEL.search(l):
            return False, None
        # "SSC Percentage / CGPA" accepts a percentage: only a GPA-only label is left alone (15 Oct 2026)
        if re.search(r"percent|%|marks|aggregate", l) and not re.search(r"year|board|school name|institut", l) \
                and (re.search(r"percent|%", l) or not re.search(r"cgpa|\bgpa\b", l)):
            key = "marks_12th_percent" if _TWELFTH.search(l) else "marks_10th_percent" if _TENTH.search(l) else None
            v = self.profile.get(key) if key else None
            return True, (_num(v) if v not in (None, "") else None)
        return True, None

    def _needs_person(self, l: str) -> bool:
        """Labels the bot must never answer: per-entry dates, and acknowledgements that may be legal claims."""
        if PER_ENTRY_DATE.search(l):
            return True
        return bool(ACK_WORDING.search(l)) and not consent_only(l)

    def value_for(self, label: str) -> tuple[str | None, str]:
        # joined labels: "FullName*", "CompanyName*", "WorkEmail" (dotcom.co.in, 21 Oct 2026)
        p, l = self.profile, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label).lower()
        links = p.get("links") or {}
        # signing an NDA (typing your name to agree) is a legal act: only for companies Ashu approved one by one
        if re.search(r"\bnda\b|non-?disclosure|confidentiality agreement", l):
            url = self.pg.url.lower()
            ok = any(re.search(rf"\b{re.escape(c.lower())}\b", url) for c in p.get("nda_approved_companies") or [])
            return (p.get("name"), "NDA approved by Ashu") if ok and re.search(r"full name|your name|sign", l) else (None, "")
        handled, val = self._school_level(l)
        if handled:
            return (val, "profile") if val is not None else (None, "")
        if self._needs_person(l):
            return None, ""
        if WORK_AUTH_Q.search(l) and not CANDIDATE_COUNTRY_Q.search(l):
            val = self._work_auth_value(l)
            return (val, "rules") if val is not None else (None, "")
        table = [
            (r"\bfirst name|given name|^first\s*\*?$", p.get("first_name")), (r"\blast name|surname|family name|^last\s*\*?$", p.get("last_name")),
            (r"^(state|state ?/ ?province|province)\s*\*?$", p.get("state")),   # Ecom Solutions: "First", "Last", "State *" (13 Oct 2026)
            (r"full name|legal name|^name\b(?! of)|your name|candidate name|^name\*?$", p.get("name")),
            (r"e-?mail", p.get("email")), (r"phone|mobile|contact (no|number)|whatsapp|^your number\b|^number\s*\*?$", p.get("phone_national")),
            (r"resume (link|url)|cv (link|url)|link to (your )?(resume|cv)", links.get("resume")),   # a shared Drive link, when set
            (r"linkedin", links.get("linkedin")), (r"github", links.get("github")),
            (r"portfolio|website|personal (site|url)", links.get("portfolio") or links.get("github")),
            (r"\bcity\b|\btown\b|current location|location", p.get("location")), (r"postal|post ?code|pin ?code|zip", p.get("postal_code")),
            (r"address", p.get("address_line1")), (r"current (company|employer|organi[sz]ation)|^company name\s*\*?$", p.get("current_company")),
            (r"current (title|designation|role|position)", p.get("current_title")),
            (r"^degree\b|degree name|qualification", p.get("degree")),
            (r"institut|school|university|college", p.get("school")),
            (r"major|department|field of study|specialization|stream|branch", p.get("major")),
            (r"street|address line|^address", p.get("address_line1")),
            (r"skill ?set|key skills|^skills", ", ".join((p.get("skills_primary") or [])[:12])),
            (r"cgpa|gpa|percentage", p.get("gpa")), (r"year of (passing|graduation)|graduation year|passing year", p.get("graduation_year")),
        ]
        short = len(re.sub(r"[*:()\s]+", " ", l).strip()) <= 40 and "?" not in l
        if short:                                   # "City", "School" - never words inside a longer question
            for pat, val in table:
                if val and re.search(pat, l):
                    return str(val), "profile"
        fixed = self._question_value(l)
        if fixed is not None:
            return fixed, "rules"
        ans = rule_answer(label, [], "text", p)
        if ans and ans.answer_text:
            return ans.answer_text, "rules"
        return None, ""

    def _question_value(self, l: str) -> str | None:
        """Common application-form questions, answered from facts Ashu confirmed (6 Oct 2026)."""
        p = self.profile
        # post-graduation / master's fields: Ashu has no PG degree - never the B.Tech CGPA (VOPA got 7.22, 15 Oct 2026)
        if re.search(r"post[- ]?grad|\bpg\b|\bmasters?\b|m\.?\s?tech|\bmba\b|\bm\.?sc\b|\bmca\b", l) and \
                re.search(r"percent|cgpa|gpa|grade|marks|year|college|universit|institut|degree|specializ", l):
            return "NA"
        handled, val = self._school_level(l)
        if handled:
            return val
        if self._needs_person(l):
            return None
        if WORK_AUTH_Q.search(l) and not CANDIDATE_COUNTRY_Q.search(l):
            return self._work_auth_value(l)
        table = [
            (r"preferred (first )?name", p.get("first_name")),
            # "Job Applied for" / "Position Applying For" as a text box (Clavrit, Skillridge 15 Oct 2026): the posting's title
            (r"(job|position|role|post) (applied|applying) for|applying for (the )?(position|role|job|post)\b", self.role or None),
            (r"^subject\s*\*?$", f"Application for {self.role}" if self.role else None),   # dotcom.co.in, 21 Oct 2026
            (r"^location( \(city\))?\b|^city\b|current city", p.get("city")),
            (r"enrolled in .{0,30}university in the past", "No"),
            (r"^school\b|school name|university", self._school_short()),
            (r"previously (been )?(employed|worked)|ever (been )?(employed|worked) (at|for|by)|employed by .{0,40} before", "No"),
            # acknowledgements: only privacy / terms / data-processing ones (legal claims were sent to the person above)
            (r"please confirm receipt|i understand that|by selecting .i acknowledge|^(personal information|privacy|data protection) (policy|notice)\b", "Confirmed"),
            (r"countr(y|ies).{0,40}(right to work|authori[sz]ed|residence|reside)|current country|country of residence|^country\b", "India"),
            (r"source of your right to work|basis of your (right|authori[sz]ation)", "Citizen"),
            (r"how (should|can|do) we (communicate|contact|reach)|preferred (method of )?(communication|contact)", "Email"),
            (r"located in india|based in india|reside in india", "Yes"),
            (r"located in one of the following countries|sanctioned|embargoed|belarus|cuba|iran|north korea|syria|crimea", "No"),
            (r"at least 18|18 years of age|over 18", "Yes"),
            (r"referred (to this|by)|referral|employee referr", "No"),
            (r"gender identity|self-identification of gender|^gender\b", p.get("gender")),
            (r"family members?|relatives?|personal relationships?.{0,40}(at|with)|outside business activit", "No"),
            (r"employment agreements?|non-?compete|restrictive covenant|non-?solicit", "No"),
            (r"open source|github|portfolio of (your )?work|links? (to|of) (your )?(projects|work)", (p.get("links") or {}).get("github")),
            (r"primary programming language", self._primary_language()),
            (r"^company( name)?\b", p.get("current_company")), (r"^(job )?title\b", p.get("current_title")),
            (r"^discipline|field of study|major", p.get("major")),
            (r"name of your (current|present) (employer|company|organi[sz]ation)|current (\(or most recent\) |or most recent )?employer|current place of work|last organi[sz]ation|current organi[sz]ation|current company", p.get("current_company")),
            (r"\b(were|are|was) you (refer+ed|recommended) by|refer+al from (an )?(employee|someone)|refer+ed by (a|an) ", "No"),
            (r"current (\(or most recent\) |or most recent )?(job )?title", p.get("current_title")),
            (r"(currently|ever) (or have you ever )?been employed (by|at|with)|have you (ever )?(worked|been employed) (for|at|with)", "No"),
            (r"(what )?year (did|do|will) you graduate|graduation year|year of (graduation|passing)", str(p.get("graduation_year") or "") or None),
            (r"what do you identify as|gender identity|^gender\b|^sex\b", p.get("gender")),
            (r"^age\b|your age\b|how old", str(p.get("age")) if p.get("age") else None),
            (r"preferred pronouns?|^pronouns?\b", p.get("pronouns")),
            (r"^organi[sz]ation name|^(latest |current |last )?employer('?s)? name|^company name\b", p.get("current_company")),
            (r"(most recent|current ?/ ?most recent|current or most recent|latest|last) (employer|company|organi[sz]ation)", p.get("current_company")),
            (r"(most recent|current ?/ ?most recent|latest) (job )?(title|designation|role)", p.get("current_title")),
            (r"^name of (the )?(college|university|institut)", p.get("school")),
            (r"preferred last name|preferred surname", p.get("last_name")), (r"preferred first name", p.get("first_name")),
            (r"currently an employee of|are you a current employee", "No"),
            (r"(read|reviewed|acknowledge).{0,40}privacy (notice|policy|statement)|consent to .{0,40}processing (my|your) data", "Confirmed"),
            (r"do you know anyone (who works )?(at|in)|know (any|some)one (at|working)", "No"),
            (r"enrolled in .{0,30}university in the past", "No"),
            (r"where are you currently (located|based)|current(ly)? located", f"{p['location']}, India" if p.get("location") else None),
            (r"home (zip|postal|pin)", str(p.get("postal_code") or "") or None),
            (r"desired (annual )?compensation|expected (annual )?compensation|compensation expectation", self._ctc_text("expected", l)),
            (r"current (total )?(annual )?compensation|curr+ent (ctc|salary)", self._ctc_text("current", l)),
            (r"^(job|employment) duration|duration (at|with|in) (your )?(current|latest|last)", p.get("current_job_duration")),
            (r"^designation( name)?\b|current designation", p.get("current_title")),
            (r"previously applied (at|to|with)|applied (to|at|with) .{0,30} (before|in the past)", "No"),
            (r"open to work(ing)? on (the )?weekends|work on weekends|weekend work", "Yes" if p.get("weekend_work_if_needed") else None),
            (r"bring your own laptop|own laptop|byod", "Yes, definitely"),
            (r"^(enter )?days ?/ ?months?$|notice period.{0,10}(days|in days)", str(p.get("notice_period_days") or "")),
            (r"deemed export|export control (rule|regulation)s? (affect|apply)", "No"),   # India roles, Indian citizen (Ashu, 7 Oct 2026)
            (r"^(work|job|employment) type\b|type of (employment|job|work)|preferred employment", "Full-time"),
            # Ashu (6 Oct 2026): uses AI when stuck - drafts a plan, has AI critique it, then picks the best one himself
            (r"how (do )?you use ai tools|describes how you use ai", "I regularly use AI tools to speed up existing tasks"),
            # start/end month & year of an employment/education entry: never guessed (PER_ENTRY_DATE -> the person)
        ]
        for pat, val in table:
            if val and re.search(pat, l):
                return str(val)
        return None

    def _note(self, label, value, source):
        log.info("    %-45s -> %s  [%s]", label[:45], str(value)[:70], source)
        # a field that failed on a first try (dropdown still loading) and then got filled is no longer unanswered
        self.result.filled = [f for f in self.result.filled if not (f["source"] == "unknown" and f["label"] == label)]
        self.result.filled.append({"label": label, "value": value, "source": source})

    # ------------------------------------------------------------------ flow
    def dismiss_cookies(self) -> None:
        """Cookie banners lay a layer over the page that swallows clicks (Zoho: a freeze layer). Click the exact
        button text, on any element type (Zoho uses <lyte-button>), preferring the privacy-friendly choice."""
        for label in ("Decline non-essential", "Reject all", "Reject All", "Decline", "Necessary only", "Only necessary",
                      "Accept all", "Accept All", "Accept", "Accept Cookies", "I agree", "Got it", "OK", "Allow all"):
            for el in self.pg.get_by_text(label, exact=True).all():
                try:
                    if el.is_visible():
                        el.click(timeout=3000); time.sleep(1.5)
                        log.info("  cookie banner: %s", label)
                        return
                except Exception:
                    continue

    def open_form(self) -> bool:
        pg = self.pg
        def has_form():
            # a resume upload, or several visible fields next to a Submit button (Zoho uses plain text boxes for email)
            if pg.locator("input[type='file']").count():
                return True
            visible = [e for e in pg.locator("input[type='text'], input[type='email'], input[type='tel'], textarea").all()
                       if e.is_visible() and not e.evaluate(SIDE_WIDGET_JS)]
            return len(visible) >= 3 and pg.get_by_text(re.compile(r"submit( application)?", re.I)).count() > 0
        if has_form():
            return True
        direct = pg.locator("a[href*='/jobs/apply/'], a[href*='/apply/']").first
        if direct.count():
            href = direct.get_attribute("href")
            base = "/".join(pg.url.split("/")[:3])
            pg.goto(href if href.startswith("http") else base + href, wait_until="domcontentloaded"); time.sleep(4)
            if has_form():
                return True
        for pat in (r"^\s*apply( now| for this job)?\s*!?\s*$", r"i.?m interested", r"apply"):
            els = []
            for el in pg.locator("a, button").all():
                try:
                    if el.is_visible() and re.search(pat, el.inner_text().strip(), re.I):
                        href = (el.get_attribute("href") or "").strip()
                        if href.lower().startswith("mailto:"):
                            # an email-only "Apply": never click it (macOS opens Apple Mail); note the address so
                            # the outreach step can email a tailored resume from Ashu's Gmail instead
                            self.mailto = href[7:].split("?")[0]
                            log.info("  Apply is an email link (%s): not clicked; noted for Gmail outreach", self.mailto)
                            continue
                        els.append(el)
                except Exception:
                    continue
            # a link wrapping a button is ONE control (Infyni: <a><button>Apply Now</button></a>): keep the innermost
            els = [el for el in els if not el.evaluate("e => !!e.querySelector('a, button')")] or els
            if len(els) > 1:
                # a listing page with one Apply button per job: take the button in THIS role's card, never the
                # first one (InCruiter, 21 Oct 2026: "AI Full Stack Developer" went in as "Quality & Compliance")
                words = [w for w in re.findall(r"[a-z]+", (self.role or "").lower()) if len(w) > 2 and w not in ("and", "the", "with")]
                # the job card = the largest block around this button that holds no other Apply button (the nearest
                # text block is often just the bullet list, without the title: Infyni, 21 Oct 2026)
                card = """e => { const label = e.innerText.trim().toLowerCase();
                           const count = n => [...n.querySelectorAll('a, button')].filter(x => x.innerText.trim().toLowerCase() === label
                                                                                          && !x.querySelector('a, button')).length;
                           let n = e; while (n.parentElement && count(n.parentElement) <= 1) n = n.parentElement;
                           return (n.innerText || '').slice(0, 600); }"""
                scored = []
                for el in els:
                    try:
                        t = el.evaluate(card).lower()
                    except Exception:
                        t = ""
                    head = next((x for x in t.splitlines() if x.strip()), "")       # the card's title line
                    # a title word counts 10, a word elsewhere in the card 1 ("agentic" in an AI Security card's
                    # bullets tied it with the Agentic AI Engineer card, Infyni 21 Oct 2026)
                    scored.append((sum(10 if re.search(rf"\b{w}", head) else 1 if re.search(rf"\b{w}", t) else 0 for w in words), el))
                best = max((s for s, _ in scored), default=0)
                top = [el for s, el in scored if s == best]
                if not words or best < 10 * max(1, (len(words) + 1) // 2) or len(top) > 1:
                    log.info("  %d Apply buttons and none clearly for %r: leaving it to Ashu", len(els), self.role)
                    return False
                els = top
            for el in els:
                try:
                    if el.is_visible() and re.search(pat, el.inner_text().strip(), re.I):
                        href = el.get_attribute("href") or ""
                        if href and not href.startswith(("#", "javascript")) and "apply" in href.lower():
                            pg.goto(href if href.startswith("http") else pg.url.split("/", 3)[0] + "//" + pg.url.split("/")[2] + href,
                                    wait_until="domcontentloaded"); time.sleep(4)
                        else:
                            new = self._click_maybe_new_tab(el)
                            if new is not None:                  # the form opened in a new tab: work there
                                self.pg = pg = new; pg.wait_for_load_state("domcontentloaded"); time.sleep(3)
                            else:
                                time.sleep(5)
                        if has_form():
                            return True
                        break
                except Exception:
                    continue
        return has_form()

    def _click_maybe_new_tab(self, el, timeout: int = 6000):
        """Click; return the tab this click opened, or None. Only that tab is ever switched to: picking
        'the last tab' could grab another company's form left open earlier."""
        clicked = False
        try:
            with self.pg.context.expect_page(timeout=timeout) as info:
                el.click(); clicked = True
            page = info.value
        except Exception:
            if not clicked:
                raise                                    # the click itself failed
            return None                                  # no new tab within the timeout: the form is on this page
        self._opened.append(page)
        return page

    def close_extra_pages(self, keep=None, everything: bool = False) -> None:
        """Close the tabs this application opened (never the tab it started in, nor `keep`). Tabs of other
        applications in the same browser (company-apply --hold keeps one per form) are left alone unless
        everything=True, which closes every tab in the context except the original (and `keep`)."""
        if everything and self._origin is not None:
            try:
                for p in self._origin.context.pages:
                    if p is not self._origin and p is not keep and not p.is_closed():
                        p.close()
            except Exception:
                pass
        for p in list(self._opened):
            if p is self._origin or p is keep:
                continue
            try:
                if not p.is_closed():
                    p.close()
            except Exception:
                pass
            self._opened.remove(p)

    def fill(self, again: bool = False) -> None:
        pg = self.pg
        # read every file field's label first: an upload re-renders forms (Greenhouse) and stales the other handles
        labels = []
        for f in pg.locator("input[type='file']").all():
            try:
                labels.append(f.evaluate(LABEL_JS, timeout=5000).lower())
            except Exception:
                labels.append("")
        for i, lab in enumerate([] if again else labels):
            f = pg.locator("input[type='file']").nth(i)
            if i == 0 or re.search(r"resume|cv", lab):
                if not re.search(r"cover|photo|image|picture", lab):
                    f.set_input_files(self.resume); self._note(lab or "Resume", os.path.basename(self.resume), "tailored resume")
                    time.sleep(3)
                    try:                                    # sites parse the resume to prefill the form (Zoho greys the page)
                        pg.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    time.sleep(3)
            elif re.search(r"cover", lab):
                # a cover-letter upload (TechAlly Labs, 22 Oct 2026): a one-page letter for this role, never the resume
                try:
                    cl = cover_letter_pdf(self.role, self.pg.url, self.profile, Path(self.resume).parent)
                    f.set_input_files(str(cl)); self._note(lab, cl.name, "cover letter")
                    time.sleep(2)
                except Exception as e:
                    log.warning("    cover letter not attached: %s", str(e)[:100])
        for el in pg.locator("input:not([type='hidden']):not([type='file']):not([type='submit']):not([type='button']), textarea, select").all():
            try:
                if not el.is_visible() or el.get_attribute("aria-hidden") == "true":
                    continue                                  # react-select's hidden "required" twin: filling it sets nothing
                typ = (el.get_attribute("type") or el.evaluate("e => e.tagName")).lower()
                name = (el.get_attribute("name") or "").lower()
                if re.search(r"gotcha|honeypot|newsletter|captcha|search", name) or typ == "search":
                    continue
                if el.evaluate(SIDE_WIDGET_JS):
                    continue                                  # job-alert / newsletter / search box, not the application
                label = el.evaluate(LABEL_JS, timeout=5000)
                if not label:
                    continue
                if re.search(r"leave (this|it)( field)? (blank|empty)|do not fill|don.t fill|if you are (a )?human", label, re.I):
                    continue                                  # visible spam trap (Akantik, 15 Oct 2026): filling it flags the form
                if typ in ("checkbox",):
                    if re.search(r"current(ly)? (role|work|position|employ)|i currently work", label, re.I):
                        if el.is_checked():
                            continue
                        # only the block whose company is the current employer; past jobs are left for the person
                        company = el.evaluate(BLOCK_COMPANY_JS)
                        if company_matches(company, self.profile.get("current_company") or ""):
                            el.check(force=True); self._note(label, "checked", "profile")
                        else:
                            self._unknown(f"{label} ({company or 'company not filled'})", [])
                        continue
                    if re.search(r"i (agree|accept|consent|acknowledge|confirm|understand|certify|declare)|privacy|terms|consent", label, re.I) \
                            and not el.is_checked():
                        if consent_only(label):
                            el.check(force=True); self._note(label, "checked", "consent")
                        else:                                     # may be a legal claim: the person decides
                            self._unknown(label, [])
                    continue
                if typ == "radio":
                    continue                                          # handled per group below
                if el.get_attribute("role") == "combobox" or el.get_attribute("aria-autocomplete"):
                    self._combobox(el, label)
                    continue
                if typ == "select":
                    if el.input_value() and not re.search(r"select|choose|^$", el.evaluate("e => e.options[e.selectedIndex]?.text || ''"), re.I):
                        continue
                    opts = [o.strip() for o in el.locator("option").all_inner_texts()]
                    real = [o for o in opts if o and not re.search(r"^(select|choose|--)", o, re.I)]
                    pick = self._pick(label, real)
                    if pick is None and re.search(r"select services|services? (required|interested)", label, re.I):
                        continue                                      # the site's sales/quote form, not the job application
                    if pick is None:
                        self._unknown(label, real); continue
                    el.select_option(label=pick); self._note(label, pick, "profile/rules"); continue
                cur = el.input_value().strip()
                val, src = self.value_for(label)
                if cur:
                    if val is not None and src == "profile" and cur != val and not re.search(r"cover|message|summary", label, re.I):
                        el.fill(""); el.fill(val); self._note(label, val, "profile (replaced autofill)")
                    continue
                if val is None and re.search(r"cover letter|why (do you|are you)|about yourself|tell us about you\b|describe yourself|\bbio\b|message|additional information|summary|introduction|intro\b|your background\b", label, re.I):
                    canned = rule_answer("Why should we hire you?", [], "text", self.profile)
                    val, src = (canned.answer_text, "canned") if canned else (None, "")
                if val is None:
                    if el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true" or "*" in label:
                        self._unknown(label, [])
                    continue
                if typ == "textarea" or (typ == "text" and re.search(r"ctc|salary|notice|experience", label, re.I) and el.get_attribute("type") != "number"):
                    val = with_unit(label, val)
                if el.get_attribute("type") == "number":
                    # a number box takes digits only: "INR 7.5 LPA" -> "7.5" (Smallest.ai, 22 Oct 2026)
                    m = re.search(r"\d+(?:\.\d+)?", str(val))
                    if not m:
                        self._unknown(label, []); continue
                    val = m.group(0)
                el.fill(val); self._note(label, val, src)
            except Exception as e:
                log.warning("    field error: %s", str(e)[:100])
        # custom dropdowns (Zoho Recruit <lyte-dropdown>): read label, open, pick an option by the same rules
        for dd in pg.locator("lyte-dropdown").all():
            try:
                if not dd.is_visible():
                    continue
                cur = dd.inner_text().strip()
                if cur and not re.search(r"^-?none-?$|select", cur, re.I):
                    continue
                label = dd.evaluate(LABEL_JS)
                if not label or re.search(r"dial code|country code", label, re.I):
                    continue
                dd.click(); time.sleep(0.8)
                items = [i for i in pg.locator("lyte-drop-item").all() if i.is_visible()]
                names = [i.inner_text().strip() for i in items]
                is_place = re.search(r"zip|postal|pin ?code|\bcity\b|town|district|state|province", label, re.I)
                # address lookups list "Locality, City, District, State, India - PIN": never take a first "Delhi" match
                pick = None if is_place else self._pick(label, [n for n in names if n and not re.search(r"^-?none-?$", n, re.I)])
                if is_place and self.profile.get("postal_code"):
                    pin = str(self.profile["postal_code"])
                    box = next((b for b in pg.locator("lyte-drop-box input, .lyteDropdownSearch input, lyte-search input, "
                                                      "input[placeholder*='Search' i]").all() if b.is_visible()), None)
                    if box:
                        box.fill(""); box.type(pin, delay=60); time.sleep(1.8)
                        items = [i for i in pg.locator("lyte-drop-item").all() if i.is_visible()]
                        names = [i.inner_text().strip() for i in items]
                        want = [c.lower() for c in (self.profile.get("city_choices") or [])]
                        with_pin = [n for n in names if pin in n]
                        pick = next((n for n in with_pin if any(w in n.lower() for w in want)), None) or (with_pin[0] if with_pin else None)
                if pick is None and is_place:
                    # searchable list (Zip / City / State): type each candidate and take the first match
                    for q in self._search_terms(label):
                        box = next((b for b in pg.locator("lyte-drop-box input, .lyteDropdownSearch input, lyte-search input, "
                                                          "input[placeholder*='Search' i]").all() if b.is_visible()), None)
                        if not box:
                            break
                        box.fill(""); box.type(q, delay=60); time.sleep(1.5)
                        items = [i for i in pg.locator("lyte-drop-item").all() if i.is_visible()]
                        names = [i.inner_text().strip() for i in items]
                        hit = next((n for n in names if n.lower().startswith(q.lower())), None) or \
                              next((n for n in names if q.lower() in n.lower()), None)
                        if hit:
                            pick = hit
                            break
                if pick is None:
                    pg.keyboard.press("Escape")
                    if "*" in label:
                        self._unknown(label, names)
                    continue
                items[names.index(pick)].click(); time.sleep(0.5)
                self._note(label, pick, "profile/rules")
            except Exception as e:
                log.warning("    dropdown error: %s", str(e)[:100])
        self._checkbox_groups()
        self._toggle_groups()
        # radio groups
        names = {r.get_attribute("name") for r in pg.locator("input[type='radio']").all() if r.is_visible()}
        for n in filter(None, names):
            group = pg.locator(f"input[type='radio'][name='{n}']")
            if any(r.is_checked() for r in group.all()):
                continue
            q = group.first.evaluate("e => { const f = e.closest('fieldset'); const l = f && f.querySelector('legend'); "
                                     "return ((l && l.innerText) || (e.closest('div') && e.closest('div').parentElement.innerText.split('\\n')[0]) || '').trim(); }")
            opts = [r.evaluate(LABEL_JS) for r in group.all()]
            pick = self._pick(q, opts)
            if pick is None:
                self._unknown(q, opts); continue
            group.nth(opts.index(pick)).check(force=True); self._note(q, pick, "rules")

    def _toggle_groups(self) -> None:
        """Ashby (and others) render yes/no questions as two toggle buttons, not radios (Cerebras' required 'Open to
        relocation if necessary', 10 Oct 2026). Answer them from the profile through the rule engine."""
        groups = self.pg.evaluate(r"""() => {
          const out = []; const seen = new Set();
          for (const b of document.querySelectorAll('button')) {
            if (!/^(yes|no)$/i.test((b.innerText || '').trim())) continue;
            const box = b.parentElement; if (!box || seen.has(box)) continue;
            const bs = [...box.children].filter(x => x.tagName === 'BUTTON' && /^(yes|no)$/i.test((x.innerText || '').trim()));
            if (bs.length !== 2) continue;
            seen.add(box);
            let q = '', n = box;
            for (let i = 0; i < 4 && n && !q; i++) { n = n.parentElement; const l = n && n.querySelector('label'); if (l) q = l.innerText; }
            const sel = bs.some(x => x.getAttribute('aria-pressed') === 'true' || x.getAttribute('aria-checked') === 'true'
                                     || /(^|[_-])(active|selected|checked)/i.test(x.className));
            box.setAttribute('data-zg-toggle', String(out.length));
            out.push({q: (q || '').replace(/\s+/g, ' ').trim(), sel});
          }
          return out; }""")
        for i, g in enumerate(groups):
            if g["sel"] or not g["q"]:
                continue
            ans = rule_answer(g["q"].rstrip("*: "), ["Yes", "No"], "radio", self.profile)
            pick = ans.answer_text if ans and ans.selected_option_indices else None
            if pick is None:
                if "*" in g["q"]:
                    self._unknown(g["q"], ["Yes", "No"])
                continue
            try:
                self.pg.locator(f"[data-zg-toggle='{i}'] > button", has_text=pick).first.click(); time.sleep(0.4)
                self._note(g["q"], pick, "rules")
            except Exception as e:
                log.warning("    toggle error: %s", str(e)[:100]); self._unknown(g["q"], ["Yes", "No"])

    def _checkbox_groups(self) -> None:
        """Greenhouse renders checkbox questions as <fieldset><legend>question</legend> with styled (hidden) inputs:
        consent/acknowledgement boxes get ticked, "how did you hear" picks from profile.how_heard, the rest go to the user."""
        groups = self.pg.evaluate("""() => {
          const out = {};
          for (const e of document.querySelectorAll("input[type='checkbox']")) {
            const f = e.closest('fieldset'); if (!f) continue;
            const lg = f.querySelector('legend'); if (!lg) continue;
            const q = lg.innerText.replace(/\\s+/g, ' ').trim();
            const lab = (e.id && document.querySelector('label[for="' + e.id + '"]')) || e.closest('label');
            (out[q] = out[q] || []).push({id: e.id, name: e.name, label: ((lab && lab.innerText) || e.value || '').trim(), checked: e.checked});
          }
          return out; }""")
        for q, boxes in groups.items():
            if any(b["checked"] for b in boxes) or not boxes:
                continue
            labels = [b["label"] for b in boxes]
            pick = None
            if HEARD_Q.search(q) or re.search(r"source", q, re.I):
                for w in (self.profile.get("how_heard") or []) + ["Job Board", "LinkedIn", "Company Website", "Website", "Web", "Other"]:
                    pick = next((l for l in labels if w.lower() in l.lower()), None)
                    if pick:
                        break
            elif re.search(r"how (should|can|do) we (communicate|contact|reach)|preferred (method of )?(communication|contact)", q, re.I):
                pick = next((l for l in labels if re.fullmatch(r"e-?mail", l.strip(), re.I)), None)
            elif re.search(r"acknowledge|consent|i (agree|accept|confirm|understand)|privacy|(information|data) (policy|notice)|by (checking|clicking)|have read|"
                           r"process(es|ing)? (of )?(my )?(personal )?data", q, re.I):
                if not consent_only(q):                   # may be a legal claim: the person decides
                    self._unknown(q, labels); continue
                pick = next((l for l in labels if re.search(r"acknowledge|agree|accept|consent|confirm|^yes|understand", l, re.I)), None) \
                       or (labels[0] if len(labels) == 1 else None)
            if pick is None:
                if "*" in q:
                    self._unknown(q, labels)
                continue
            b = boxes[labels.index(pick)]
            try:
                loc = self.pg.locator(f"[id='{b['id']}']") if b["id"] else self.pg.locator(f"input[type='checkbox'][name='{b['name']}']").nth(labels.index(pick))
                lab = self.pg.locator(f"label[for='{b['id']}']") if b["id"] else None
                if lab is not None and lab.count() and lab.first.is_visible():
                    lab.first.click()
                if not loc.is_checked():
                    loc.check(force=True)
                self._note(q, pick, "rules")
            except Exception as e:
                log.warning("    checkbox error: %s", str(e)[:100]); self._unknown(q, labels)

    def _pick(self, label: str, options: list[str]) -> str | None:
        if not options:
            return None
        l = label.lower()
        # a US-state list with an "N/A" entry (New Relic, 21 Oct 2026): Ashu lives in India
        if re.search(r"^\s*state\b", l) and any(re.fullmatch(r"\s*alabama\s*", o, re.I) for o in options) \
                and str(self.profile.get("country") or "India").lower() != "united states":
            hit = next((o for o in options if re.fullmatch(r"\s*(n/?a|not applicable|outside (the )?(us|u\.s\.|united states))\s*", o, re.I)), None)
            if hit:
                return hit
        # experience level offered without a usable label ("Intern / Fresher / Experienced", Triosoft, 21 Oct 2026):
        # Ashu has 2 years of work experience
        if any(re.fullmatch(r"\s*fresher\s*", o, re.I) for o in options):
            hit = next((o for o in options if re.match(r"\s*experienced\b", o, re.I)), None)
            if hit:
                return hit
        # college dropdowns: NSUT when listed, else the list's "Other" (Ashu, 6 Oct 2026)
        if re.search(r"(undergraduate|graduation|bachelor'?s?)? ?(institution|university|college)( name)?\b", l) and not re.search(r"year|gpa|cgpa|percent|degree|major", l):
            hit = next((o for o in options if re.search(r"netaji|nsut", o, re.I)), None) or \
                  next((o for o in options if re.search(r"^\s*others?\b", o, re.I)), None)
            if hit:
                return hit
        # a privacy / data-transfer acknowledgement offered as a dropdown ("Point of Data Transfer": Acknowledge/Confirm,
        # Lynx 13 Oct 2026): same rule as consent checkboxes, only for privacy-type wording, never legal claims
        if consent_only(label):
            ack = next((o for o in options if re.search(r"^\s*(i )?(acknowledge|confirm|agree|accept|consent|yes)\b", o, re.I)), None)
            if ack:
                return ack
        # US export-control (EAR) citizenship check (Cerebras, chip and defence-adjacent firms): a factual answer from
        # profile.nationality - never "U.S. person" for a non-US national
        if re.search(r"export administration|\bu\.?s\.? person\b|deemed export", l + " " + " ".join(options), re.I):
            nat = (self.profile.get("nationality") or "").lower()
            if nat and not re.search(r"\b(american|united states|u\.?s\.?)\b", nat):
                sanctioned = bool(re.search(r"cuba|iran|north korea|syria", nat))
                want = (r"not a u\.?s\.? person.{0,40}\bam a\b" if sanctioned else
                        r"not a u\.?s\.? person.{0,40}(not a current|am not a citizen|not a citizen)")
                hit = next((o for o in options if re.search(want, o, re.I)), None)
                if hit:
                    return hit
        if HEARD_Q.search(label):
            for w in (self.profile.get("how_heard") or []) + ["Job Board", "LinkedIn", "Company Website", "Website", "Web", "Other"]:
                hit = next((o for o in options if w.lower() in o.lower()), None)
                if hit:
                    return hit
        handled, val = self._school_level(l)
        if handled or self._needs_person(l) or (WORK_AUTH_Q.search(l) and not CANDIDATE_COUNTRY_Q.search(l)):
            want = val if handled else None if self._needs_person(l) else self._work_auth_value(l)
            if want is None:
                return None
            return next((o for o in options if o.strip().lower() == want.lower()), None) or \
                   next((o for o in options if re.match(rf"{re.escape(want)}\b", o.strip(), re.I)), None)
        if re.search(r"role|position|job title|applying for|department", l) and self.role:
            want = self.role.lower()
            for pat in (re.escape(want), r"back-?end|backend", r"full[- ]?stack", r"\bai\b|machine learning|ml", r"software engineer"):
                hit = next((o for o in options if re.search(pat, o, re.I)), None)
                if hit:
                    return hit
        buckets = [(o, re.search(r"(\d+)\s*(?:-|to|–)\s*(\d+)\s*(?:years?|yrs)|<\s*(\d+)\s*years?|(\d+)\s*\+\s*years?", o, re.I)) for o in options]
        if sum(1 for _, m in buckets if m) >= 2:                      # experience buckets offered without a real label
            yrs = 2
            for o, m in buckets:
                if m and m.group(1) and int(m.group(1)) <= yrs <= int(m.group(2)):
                    return o
        cur_lpa = self.profile.get("current_ctc_lpa")
        if re.search(r"current (total )?(annual )?(compensation|ctc|salary)", l) and any(re.search(r"lpa|lakh", o, re.I) for o in options):
            if cur_lpa in (None, ""):
                return None
            cur_lpa = float(cur_lpa)
            for o in options:                                     # pay bands such as "<10 LPA", "10-15 LPA"
                m = re.search(r"<\s*(\d+)", o)
                if m and cur_lpa < float(m.group(1)):
                    return o
                n = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", o)]
                if len(n) >= 2 and n[0] <= cur_lpa < n[1]:
                    return o
        rupee_bands = [(o, [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]{4,}", o)]) for o in options]
        if re.search(r"compensation|ctc|salary", l) and sum(1 for _, n in rupee_bands if len(n) >= 2) >= 2:
            lpa = self.profile.get("expected_ctc_lpa" if re.search(r"desired|expect", l) else "current_ctc_lpa")
            if lpa in (None, ""):
                return None
            want_inr = float(lpa) * 100000                    # rupee bands such as "7,00,000 to 10,00,000"
            return next((o for o, n in rupee_bands if len(n) >= 2 and n[0] <= want_inr <= n[1]), None)
        if re.search(r"(employee|contractor) (or contractor )?(at|of|with)\b", l) and re.search(r"currently|are you", l):
            return next((o for o in options if re.match(r"no\b", o, re.I)), None)
        if re.search(r"hispanic|latino|latinx", l) or re.search(r"identify as transgender|^transgender", l):
            # no "No" offered (Smartsheet, 29 Oct 2026: Yes / I don't wish to answer) -> decline
            return next((o for o in options if re.match(r"no\b", o, re.I)), None) or \
                   next((o for o in options if re.search(r"don.?t wish|prefer not|decline|not to (say|answer|disclose)", o, re.I)), None)
        if re.search(r"identify my gender|gender identity|^gender\b", l) and self.profile.get("gender"):
            hit = next((o for o in options if re.fullmatch(r"(male|man)", o.strip(), re.I)), None) or \
                  next((o for o in options if re.match(r"(man|male)\b", o.strip(), re.I)), None)   # "Man, male or masculine" (Weave)
            if not hit:   # no male option offered (Smartsheet, 29 Oct 2026: Female / Non-binary / I don't wish to answer)
                hit = next((o for o in options if re.search(r"don.?t wish|prefer not|decline|not to (say|answer|disclose)", o, re.I)), None)
            if hit:
                return hit
        if re.search(r"disabilit", l) and self.profile.get("disability") is False:
            # voluntary self-identification; profile.disability is Ashu's confirmed answer (Weave, 24 Oct 2026)
            return next((o for o in options if re.match(r"no\b|i do not have|i don.t have a disab", o.strip(), re.I)), None)
        if re.search(r"veteran", l):        # "No, I am not a veteran" (Smartsheet, 29 Oct 2026); profile: not a veteran
            return next((o for o in options if re.search(r"not a (protected )?veteran|^no\b|i am not", o, re.I)), None) or \
                   next((o for o in options if re.search(r"decline|prefer not|do not wish|don.t wish", o, re.I)), None)
        if re.search(r"race|ethnicity", l):  # South Asian / Indian when listed, else decline
            return next((o for o in options if re.search(r"south asian|\bindian\b(?!.*(american|alaska|native))", o, re.I)), None) or \
                   next((o for o in options if re.search(r"decline|prefer not|do not wish|don.t wish", o, re.I)), None)
        if re.search(r"^(sms|whatsapp)$|text messages|whatsapp messages", l):
            return next((o for o in options if re.match(r"yes\b", o, re.I)), None)      # Ashu: opt in to SMS contact
        if re.search(r"familiar(ity)? with (artificial intelligence|ai)|rate .{0,30}(ai|artificial intelligence)", l):
            return next((o for o in options if re.search(r"advanced|expert|very familiar|extensive", o, re.I)), None)
        if re.search(r"building ui in react|react specifically", l):
            return next((o for o in options if re.match(r"some\b", o, re.I)), None)   # React at HeadsUp, not the main job
        if re.search(r"^race\b|ethnicity", l):
            return next((o for o in options if re.search(r"decline|prefer not|do not wish|don.t wish", o, re.I)), None)
        if re.search(r"notice", l) and options and all(re.fullmatch(r"\s*(days?|weeks?|months?)\s*", o, re.I) for o in options):
            return next((o for o in options if re.match(r"\s*days?", o, re.I)), None)     # unit picker; the number goes in its own box
        if re.search(r"employment status|current status|working status", l):
            return next((o for o in options if re.search(r"^employed|working|full[- ]?time|^experienced", o, re.I) and not re.search(r"un|self", o, re.I)), None)
        if re.search(r"qualification|degree|education", l):
            return next((o for o in options if re.search(r"bachelor|b\.?\s?tech|b\.?\s?e\b|graduat|under ?grad", o, re.I)
                         and not re.search(r"post|master|under ?graduate student", o, re.I)), None)
        if re.search(r"^(mr|salutation|title)$", l) or options[:3] == ["Mr.", "Mrs.", "Ms."]:
            return next((o for o in options if o.lower().startswith("mr")), None)
        if re.search(r"gender", l) and self.profile.get("gender"):
            return next((o for o in options if o.lower() == self.profile["gender"].lower()), None)
        if re.search(r"country", l):
            # "India +91" in phone-code lists, never "British Indian Ocean Territory +246" (Greenhouse, 22 Oct 2026)
            return next((o for o in options if o.lower() == "india"), None) or \
                   next((o for o in options if re.match(r"\s*india\b(?!n)", o, re.I)), None)
        if re.search(r"experience|years", l):
            yrs = float(self.profile.get("total_experience_years") or 0)
            for o in options:
                nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", o)]
                if len(nums) >= 2 and nums[0] <= yrs <= nums[1]:
                    return o
                if len(nums) == 1 and ("+" in o or "above" in o.lower()) and yrs >= nums[0]:
                    return o
        ans = rule_answer(label, options, "dropdown", self.profile)
        if ans and ans.selected_option_indices:
            return options[ans.selected_option_indices[0]]
        want = self._question_value(l)
        if want == "Confirmed":
            return next((o for o in options if re.search(r"acknowledge|confirm|agree|^yes|understand|accept|\bi consent\b", o, re.I)), None)
        if want:
            return next((o for o in options if o.lower() == want.lower()), None) or \
                   next((o for o in options if o.lower().startswith(want.lower()[:5])), None)
        return None

    def _combobox(self, el, label: str) -> None:
        pg = self.pg
        container = el.locator("xpath=ancestor::*[contains(@class,'select') or contains(@class,'Select')][1]")
        if container.count() and re.search(r"\S", container.first.inner_text().replace(label, "")) and \
                not re.search(r"select\.\.\.|^\s*select\s*$", container.first.inner_text(), re.I):
            return                                               # already has a value
        el.click(); time.sleep(0.8)
        names = []
        for k in range(8):                                     # options can load a few seconds after the menu opens
            names = [o.inner_text().strip() for o in pg.locator("[role='option']").all() if o.is_visible()]
            if names and not all(re.search(r"^(loading|no options)", n, re.I) for n in names):
                break
            if k in (2, 5):                                    # a click sometimes leaves react-select closed: the arrow key opens it
                try:
                    el.focus(); el.press("ArrowDown")
                except Exception:
                    pass
            time.sleep(0.7)
        want = self._question_value(label.lower())
        pick = None
        if want == "No" and names:                             # "No" is often a sentence: "I have never been employed by X..."
            pick = next((n for n in names if re.search(r"^no\b|\bnever\b|\b(have|has|do|did) not\b|\bi am not\b|\bnot (been|a|an|currently)\b", n, re.I)), None)
            if pick:
                want = None
        if pick is not None:
            pass
        elif want == "Confirmed":
            pick = next((n for n in names if re.search(r"confirm|acknowledge|^yes|agree|understand", n, re.I)), None)
        elif want:
            pick = next((n for n in names if n.lower() == want.lower()), None)
            if pick is None and len(names) > 8:            # long lists: type to filter, then take the exact / closest match
                el.type(want[:25], delay=40); time.sleep(1.5)
                names = [o.inner_text().strip() for o in pg.locator("[role='option']").all() if o.is_visible()]
                # whole-word, prefix first: "India" must not match "British Indian Ocean Territory +246"
                pick = next((n for n in names if n.lower() == want.lower()), None) or \
                       next((n for n in names if re.match(rf"{re.escape(want)}\b", n, re.I)), None) or \
                       next((n for n in names if re.search(rf"\b{re.escape(want)}\b", n, re.I)), None)
            if pick is None:
                pick = next((n for n in names if re.match(rf"{re.escape(want)}\b", n, re.I)), None) or \
                       next((n for n in names if n.lower().startswith(want.lower()[:5])), None)
            if pick is None and re.search(r"school|university", label, re.I):
                pick = next((n for n in names if re.search(r"^0 - other|^other$", n, re.I)), None)
        if pick is None and names:
            pick = self._pick(label, names)
        if pick is None and (HEARD_Q.search(label) or re.search(r"source", label, re.I)):
            for w in (self.profile.get("how_heard") or []) + ["Job Board", "LinkedIn", "Other"]:
                pick = next((n for n in names if w.lower() in n.lower()), None)
                if pick:
                    break
        if pick is None and want:                              # long lists load as you type (country, school)
            # education lists: take the real entry if it is listed, else the list's own "Other" (never leave it blank)
            edu = re.search(r"school|university|college|discipline|field of study|major", label, re.I)
            acronym = self._school_acronym()
            for term in [want[:20]] + ([t for t in (acronym, "Other") if t] if re.search(r"school|university|college", label, re.I) else
                                       ["Computer Science", "Other"] if edu else []):     # B.Tech IT: Ashu OK'd CS or Other
                el.fill(""); el.type(term, delay=50)
                for _ in range(8):
                    time.sleep(0.8)
                    names = [o.inner_text().strip() for o in pg.locator("[role='option']").all() if o.is_visible()]
                    if names and not all(re.search(r"^(loading|searching|no (options|results))", n, re.I) for n in names):
                        break
                if term == "Other":
                    pick = next((n for n in names if re.search(r"^(0 - )?other\b", n, re.I)), None)
                elif term == "Computer Science":
                    pick = next((n for n in names if re.fullmatch(r"computer science( (and|&) engineering)?", n, re.I)), None)
                elif term == acronym:
                    rx = self._school_names_rx()
                    pick = next((n for n in names if rx and re.search(rx, n, re.I)), None)
                else:
                    pick = next((n for n in names if n.lower() == want.lower()), None) or \
                           next((n for n in names if re.match(rf"{re.escape(want)}\b", n, re.I)), None) or \
                           next((n for n in names if n.lower().startswith(want.lower()[:5])), None)
                if pick:
                    break
        if pick is None:
            pg.keyboard.press("Escape")
            if "*" in label:
                self._unknown(label, names[:6])
            return
        opt = next((o for o in pg.locator("[role='option']").all() if o.is_visible() and o.inner_text().strip() == pick), None)
        if opt is None:                                        # the list re-rendered and the option is gone
            pg.keyboard.press("Escape")
            self._unknown(label, names[:6])
            return
        opt.click(); time.sleep(0.5)
        self._note(label, pick, "rules")

    def _search_terms(self, label: str) -> list[str]:
        p, l = self.profile, label.lower()
        if re.search(r"zip|postal|pin ?code", l):
            return [str(p.get("postal_code", ""))]
        if re.search(r"\bcity\b|town|district", l):
            return [str(c) for c in p.get("city_choices") or [p.get("city", "")]]
        if re.search(r"state|province|region", l):
            return [str(p.get("state", ""))]
        if re.search(r"country", l):
            return [str(p.get("country", "India"))]
        return []

    def _enter_code(self, timeout_s: int = 900) -> bool:
        """Wait for the user to drop Greenhouse's emailed code into code_file, type it, submit, and confirm."""
        path = Path(self.code_file)
        path.unlink(missing_ok=True)
        log.info("    WAITING for the security code emailed to you -> write it to %s (up to %d min)", path, timeout_s // 60)
        end = time.time() + timeout_s
        while time.time() < end:
            if path.exists() and path.read_text().strip():
                break
            time.sleep(2)
        else:
            return False
        code = re.sub(r"\s", "", path.read_text()); path.unlink(missing_ok=True)
        boxes = [b for b in self.pg.locator("input[id^='security-input'], input[name*='security' i], input[aria-label*='security' i], "
                                            "input[autocomplete='one-time-code']").all() if b.is_visible()]
        if not boxes:
            log.warning("    security code field not found"); return False
        if len(boxes) >= len(code) > 1:                    # one box per character
            for b, ch in zip(boxes, code):
                b.fill(ch)
        else:
            boxes[0].fill(code)
        time.sleep(1)
        btn = next((b for b in self.pg.locator("button[type='submit'], button").all()
                    if b.is_visible() and re.search(r"submit", b.inner_text() or "", re.I)), None)
        try:
            before, before_url = self.pg.inner_text("body"), self.pg.url
        except Exception:
            before, before_url = "", ""
        if btn:
            btn.click()
        for _ in range(20):
            time.sleep(2)
            body = self.pg.inner_text("body")
            if confirmation_seen(before, body, before_url, self.pg.url):
                return True
            if re.search(r"invalid|incorrect|expired", body, re.I) and re.search(r"code", body, re.I):
                log.warning("    the code was rejected"); return False
        return False

    def _unknown(self, label, options):
        log.info("    %-45s -> (no answer) options=%s", label[:45], options[:5])
        self.result.filled.append({"label": label, "value": None, "source": "unknown", "options": options})
        from ..ask_ashu import record
        record(label, options, "company-form", getattr(self, 'role', ''))

    def ensure_resume(self) -> int:
        """Attach the tailored resume again to every empty resume field. Called before a tab is left for Ashu:
        a rejected submit (Lever, Turgon AI) or a re-render clears the file, and he could not apply without it
        (Triosoft, 21 Oct 2026). Returns how many fields were re-attached."""
        n = 0
        for f in self.pg.locator("input[type='file']").all():
            try:
                lab = f.evaluate(LABEL_JS, timeout=5000).lower()
                if re.search(r"cover|photo|image|picture|avatar", lab) and not re.search(r"resume|cv", lab):
                    continue
                if f.evaluate("e => e.files ? e.files.length : 0") == 0:
                    f.set_input_files(self.resume); time.sleep(2); n += 1
            except Exception as e:
                log.warning("    resume re-attach failed: %s", str(e)[:100])
        if n:
            log.info("    resume re-attached to %d empty field(s)", n)
        return n

    def run(self, url: str) -> FormResult:
        try:
            return self._run(url)
        finally:
            # close the tabs this application opened; keep the form tab only when the person still has to act on it
            keep = self.pg if self.result.status in ("ready_to_submit", "needs_you") else None
            try:
                self.close_extra_pages(keep=keep)
            except Exception:
                pass

    def _find_submit(self):
        """The Submit control of the form being filled: a submit button inside the <form> that holds the resume
        or email field; only if there is none, a page-wide search (never a nav link like "Submit your CV")."""
        pg = self.pg
        def text(b):
            try:
                return (b.inner_text() or b.get_attribute("value") or "").strip()
            except Exception:
                return ""
        def vis(b):
            try:
                return b.is_visible()
            except Exception:
                return False
        forms = pg.locator("form").filter(has=pg.locator("input[type='file'], input[type='email'], input[name*='mail' i]"))
        for k in range(forms.count()):
            try:
                if forms.nth(k).evaluate(SIDE_WIDGET_JS):
                    continue                                  # never "submit" a job-alert sign-up
            except Exception:
                pass
            cands = [b for b in forms.nth(k).locator("button[type='submit'], input[type='submit'], "
                                                     "button:not([type='button']):not([type='reset'])").all() if vis(b)]
            btn = next((b for b in cands if re.search(r"submit|apply|send|finish", text(b), re.I)), None) or \
                  (cands[0] if len(cands) == 1 else None)
            if btn is not None:
                return btn
        # page-wide: real buttons first; a link only when its whole text is a submit phrase
        cands = [b for b in pg.locator("button[type='submit'], input[type='submit'], button, a[role='button'], a.s_website_form_send").all()
                 if vis(b)]
        def is_link(b):
            try:
                return b.evaluate("e => e.tagName") == "A"
            except Exception:
                return True
        cands = [b for b in cands if not is_link(b) or re.fullmatch(r"submit( application| my application)?|send( application)?", text(b), re.I)
                 or "s_website_form_send" in (b.get_attribute("class") or "")]
        return next((b for b in cands if re.search(r"^submit( application| my application)?$|submit", text(b), re.I)), None) or \
               next((b for b in cands if re.search(r"^(send|send application|apply now|finish)$", text(b), re.I)), None)

    def _form_gone(self, btn) -> bool:
        """The Submit button and every required field have left the page (the form was replaced)."""
        try:
            if btn.is_visible():
                return False
        except Exception:
            pass
        try:
            return not self.pg.evaluate("""() => [...document.querySelectorAll("input[required], textarea[required], select[required], "
                + "[aria-required='true'], input[type='file'], input[type='email']")].some(e => e.offsetParent !== null)""")
        except Exception:
            return False

    def _run(self, url: str) -> FormResult:
        self.pg.goto(url, wait_until="domcontentloaded", timeout=60000); time.sleep(5)
        self.dismiss_cookies()
        try:                                          # where the job is, read on the job page itself
            self._start_country = job_country_from(self._page_text(), self.pg.url)
        except Exception:
            self._start_country = None
        if not self.open_form():
            self.result.status, self.result.notes = "no_form", "no application form found (email-only or listing page)"
            return self.result
        self.fill()
        # answers can reveal more fields (experience level -> employer, duration, reason): one more pass for those
        if self.pg.evaluate("""() => [...document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox]), textarea, select')]
                 .some(e => e.offsetParent && !e.value && (e.required || e.getAttribute('aria-required') === 'true' || e.closest('[class*=required]')
                      || /\\*/.test((e.labels && e.labels[0] && e.labels[0].innerText) || '')))"""):
            self.result.filled = [f for f in self.result.filled if f["source"] != "unknown"]
            self.fill(again=True)
        visible_captcha = any(e.is_visible() for e in self.pg.locator(
            "input[placeholder*='captcha' i], input[placeholder*='image text' i], input[name*='captcha' i], "
            "iframe[src*='recaptcha'][title*='reCAPTCHA' i]:not([src*='size=invisible']), iframe[src*='hcaptcha'], iframe[src*='turnstile']").all())
        if visible_captcha:
            # CAPTCHAs are for a person: fill everything, never solve or bypass it
            self.result.status, self.result.notes = "needs_you", "CAPTCHA on the form: type it and press Submit yourself"
            return self.result
        # a sales / client enquiry form reached from a careers link ("Tell us about your project", "Project type",
        # "Estimated budget": Xonier 15 Oct 2026): not a job application - never fill or submit it
        labels = " | ".join(str(f.get("label") or "") for f in self.result.filled)
        if re.search(r"tell us about your project|project type|estimated budget|your budget|project budget|"
                     r"request (a|for) (quote|proposal)|get a (free )?quote", labels, re.I):
            self.result.status, self.result.notes = "skipped", "not a job application (client enquiry form)"
            return self.result
        # "Email this job" / share-with-a-friend widgets on a job page ("Recipient's Email address": Ecolab careers,
        # 15 Oct 2026): sending one is not an application
        if re.search(r"recipient'?’?s? e-?mail|friend'?’?s? e-?mail|email this job|share this job|send (this )?job to", labels, re.I):
            self.result.status, self.result.notes = "no_form", "only an 'email this job' widget found, no application form"
            return self.result
        missing = [f["label"] for f in self.result.filled if f["source"] == "unknown"]
        if missing:
            self.result.status, self.result.notes = "needs_you", f"no answer for: {missing[:3]}"
            return self.result
        self.result.status, self.result.notes = "ready_to_submit", "every field filled from profile/rules"
        if self.submit:
            # prefer the form's own submit control: Greenhouse's top "Apply" button only scrolls to the form
            btn = self._find_submit()
            if not btn:
                self.result.status, self.result.notes = "needs_you", "no Submit button found"
                return self.result
            start_url = self.pg.url
            try:
                before = self.pg.inner_text("body")       # what the page said before the click: old text is no proof
            except Exception:
                before = ""
            btn.click()
            ok = code = False
            body = ""
            for _ in range(20):                       # forms can take a while (invisible reCAPTCHA, uploads)
                time.sleep(2)
                body = self.pg.inner_text("body")
                if re.search(r"verification code was sent|enter the \d+-character code|security code", body, re.I) and \
                        not re.search(r"verification code was sent|enter the \d+-character code|security code", before, re.I):
                    code = True                       # Greenhouse emails a code to prove you are human: only the user can enter it
                    break
                if confirmation_seen(before, body, start_url, self.pg.url, self._form_gone(btn)):
                    ok = True
                    break
            if not ok and not code:                   # the thank-you page sometimes lands just after the loop: look once more
                time.sleep(5)
                try:
                    ok = confirmation_seen(before, self.pg.inner_text("body"), start_url, self.pg.url, self._form_gone(btn))
                except Exception:
                    pass
            if ok:
                self.result.status, self.result.notes = "applied", "submitted; confirmation shown"
            elif code and self.code_file and self._enter_code():
                self.result.status, self.result.notes = "applied", "submitted with emailed security code; confirmation shown"
            elif code:
                self.result.status, self.result.notes = "needs_you", "NOT submitted yet: enter the security code emailed to you, then press Submit"
            else:
                # validation errors mean the form was NOT sent: report them so the fix is obvious and a retry is safe
                errs = self.pg.evaluate("""() => [...document.querySelectorAll(".helper-text--error, [class*='error-message'], [role='alert']")]
                    .filter(e => e.offsetParent !== null && e.innerText.trim()).map(e => {
                      const w = e.closest('.field-wrapper, fieldset, .input-wrapper, div');
                      const l = w && w.querySelector('label, legend');
                      return ((l && l.innerText.trim().slice(0, 50)) || '?') + ': ' + e.innerText.trim().slice(0, 60); })""")
                # browser-native validation ("Please select an item in the list") blocks the submit without any
                # error element on the page: a visible control still :invalid means nothing was sent
                errs += self._invalid_fields()
                still_open = self._find_submit() is not None
                # fields that appear only after an earlier answer ("Notice period", "Expected salary" after "Work type":
                # iTechnosol, 16 Oct 2026): fill once more and retry the submit before handing it over
                if errs and still_open and not getattr(self, "_refilled", False):
                    self._refilled = True
                    log.info("    form rejected (%s): filling the new fields and retrying once", "; ".join(errs[:3])[:120])
                    self.fill(again=True)
                    if not [f for f in self.result.filled if f["source"] == "unknown"]:
                        btn2 = self._find_submit()
                        if btn2:
                            start_url, before = self.pg.url, self.pg.inner_text("body")
                            btn2.click()
                            for _ in range(15):
                                time.sleep(2)
                                if confirmation_seen(before, self.pg.inner_text("body"), start_url, self.pg.url, self._form_gone(btn2)):
                                    self.result.status, self.result.notes = "applied", "submitted (second pass); confirmation shown"
                                    return self.result
                    errs = self._invalid_fields() or errs
                if errs:
                    self.result.status, self.result.notes = "needs_you", "form rejected, NOT submitted: " + "; ".join(errs[:5])
                elif still_open and self.pg.locator("script[src*='recaptcha'], iframe[src*='recaptcha']").count():
                    # An invisible reCAPTCHA (Ashby) silently drops automated submits: the form is still there, filled.
                    # Not worked around on purpose - the person presses Submit (company-apply --hold keeps it open).
                    self.result.status, self.result.notes = "needs_you", ("NOT submitted: the site's invisible bot check blocked "
                                                                          "the automated Submit; the form is filled - press Submit yourself")
                else:
                    self.result.status, self.result.notes = "unconfirmed", "pressed Submit but saw no confirmation (do NOT resubmit blindly)"
        return self.result

    def _invalid_fields(self) -> list[str]:
        """'label: browser message' for each visible form control the browser still marks :invalid."""
        try:
            return [f"{e.evaluate(LABEL_JS) or e.get_attribute('name') or '?'}"[:50] + ": " + (e.evaluate("e => e.validationMessage") or "invalid")[:60]
                    for e in self.pg.locator("input:invalid, select:invalid, textarea:invalid").all()
                    if (e.get_attribute("type") or "") != "hidden" and e.is_visible()]
        except Exception:
            return []
