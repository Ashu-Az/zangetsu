"""Every Naukri DOM selector in one place so a redesign only means editing this file.

Verified against the live site on 2026-10-02 (headed Chromium). Hashed class names
such as `styles_jd-header-title__rZwM1` are matched with `[class*=...]` so the hash
suffix can change without breaking the bot.
"""

# --- global / login ---
LOGIN_LINK = "a#login_Layer"                # present only when logged out
LOGIN_USERNAME = "#usernameField"
LOGIN_PASSWORD = "#passwordField"
LOGIN_SUBMIT = "button[type='submit']"
LOGGED_IN_MARKERS = ["div.nI-gNb-drawer__bars", "img.nI-gNb-icon-img", "a[href*='/mnjuser/profile']"]

# --- search results page ---
JOB_CARD = "div.srp-jobtuple-wrapper"
CARD_TITLE = "a.title"
CARD_COMPANY = "a.comp-name, .comp-name"
CARD_EXPERIENCE = ".exp, .expwdth"
CARD_LOCATION = ".loc, .locWdth"
CARD_DESC = ".job-desc"
CARD_POSTED = ".job-post-day"
CARD_SKILLS = ".tag-li"

# --- job detail page ---
JD_TITLE = "h1[class*='jd-header-title'], h1"
JD_COMPANY_LINK = "[class*='jd-header-comp-name'] a"
JD_COMPANY = "[class*='jd-header-comp-name']"
JD_EXPERIENCE = "[class*='jhc__exp']"
JD_SALARY = "[class*='jhc__salary']"
JD_LOCATION = "[class*='jhc__loc']"
JD_STATS = "[class*='jhc__stat']"
JD_DESCRIPTION = "[class*='JDC__dang-inner-html'], [class*='job-desc']"
JD_DETAILS = "[class*='styles_details'], [class*='other-details']"
JD_EDUCATION = "[class*='education']"
JD_KEY_SKILLS = "[class*='key-skill'] a, [class*='key-skill'] span"
JD_ABOUT_COMPANY = "[class*='about-company']"
# Naukri "Job match score" (verified 2026-10-05): Early Applicant / Keyskills / Location / Work Experience,
# each with a check_circle (match) or crossMatchscore (no match) icon
MATCH_ITEM = "div[class*='MS__details']"
MATCH_OK_ICON = "i.ni-icon-check_circle"

APPLY_BUTTON = "button#apply-button, button[class*='apply-button']:not([id*='company'])"
COMPANY_SITE_BUTTON = "button#company-site-button, button[class*='company-site-button']"
ALREADY_APPLIED = "#already-applied, [class*='already-applied'], button:has-text('Applied')"
JOB_EXPIRED_TEXT = ["This job has expired", "job is no longer available", "Job not found"]
DAILY_QUOTA_TEXT = ["daily quota", "quota has been expired", "limit reached", "reached the daily limit", "reached your daily limit", "reached the maximum number of applications"]
APPLY_SUCCESS_TEXT = ["successfully applied", "Application sent", "You have applied", "applied successfully",
                      "Thank you for your responses", "Thank you for applying"]

# --- questionnaire chatbot drawer (opens after clicking Apply on some jobs) ---
DRAWER = "[id*='ChatbotContainer'], .chatbot_Drawer, [class*='chatbot_Drawer']"
DRAWER_CLOSE = ".chatbot_Drawer [class*='crossIcon'], .chatbot_Drawer .close"
USER_MESSAGES = ".chatbot_ListItem.userItem .msg, [class*='userMsg'] [class*='msg'], .chatbot_ListItem:not(.botItem) .msg"
BOT_MESSAGES = ".chatbot_ListItem .botMsg .msg, .chatbot_ListItem.botItem .msg, [class*='botMsg'] [class*='msg']"
USER_MESSAGES = ".chatbot_ListItem .userMsg .msg, .chatbot_ListItem.userItem .msg"
RADIO_CONTAINER = ".singleselect-radiobutton-container, [class*='singleselect-radiobutton']"
RADIO_INPUT = ".ssrc__radio, input[type='radio']"
RADIO_LABEL = ".ssrc__label, label"
CHECKBOX_CONTAINER = ".multiselect-checkbox-container, [class*='multiselect-checkbox']"
CHECKBOX_INPUT = ".mcc__checkbox, input[type='checkbox']"
CHECKBOX_LABEL = ".mcc__label, label"
SUGGESTOR_INPUT = ".chatbot_Drawer input[type='text'], [class*='suggestor'] input, [class*='ssc__input']"
SUGGESTOR_OPTION = "[class*='suggestor'] li, [class*='ssc__list'] li, .chatbot_Drawer ul li"
DROPDOWN = ".chatbot_Drawer select"
TEXT_INPUT = "div.textArea[contenteditable='true'], [class*='InputContainer'] [contenteditable='true'], .chatbot_Drawer [contenteditable='true']"
SEND_BUTTON = ".sendMsg, [class*='sendMsg'], .chatbot_Drawer button:has-text('Save'), .chatbot_Drawer button:has-text('Submit')"
SEND_DISABLED_CLASS = "disabled"
