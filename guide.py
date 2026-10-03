"""The "How to Use" guide. It is built from your current box names and order, so it updates itself
whenever you customize the boxes. The same guide is saved to HOW-TO-USE.md next to the app."""
import os
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
MD_PATH = os.path.join(HERE, "HOW-TO-USE.md")

ICON = {"New Lead": "📥", "Pre-Sales Video Viewed": "👀", "Pre-Qualified": "✅", "Discovery Call Scheduled": "📅",
        "Estimating/Design": "📐", "Proposal Sent": "📄", "Closed-Won": "🎉", "Nurture/Follow-Up": "🔄"}

# What to do while a customer sits in each box, and why.
BOX_STEPS = {
    "New Lead": ("Call or text them and ask 3 things: Do you own the home? What's your budget? When do you want to start? "
                 "Open the customer, press ✏️ Change their info, type the answers, then press 💾 Save and re-sort.",
                 "Remod Flow can only sort a customer when it knows those 3 answers."),
    "Pre-Sales Video Viewed": ("They watched your pricing video. Ask their budget and start date, then press Next step.",
                               "People who watched the video already know your prices, so they're warmer."),
    "Pre-Qualified": ("Send the 💬 First text right away (📋 Copy, paste in your phone, send, then ✅ I sent it). "
                      "When they pick a call time, press Next step.",
                      "The faster you answer, the more likely you win the job."),
    "Discovery Call Scheduled": ("Text them a reminder the day before. Have the call. When you've booked a visit, press Next step.",
                                 "A reminder text stops people from forgetting the call."),
    "Estimating/Design": ("Visit, measure, and plan the design. When your price is ready, press Next step.",
                          "Moving the card keeps your Possible money number honest."),
    "Proposal Sent": ("Send the price. Check in after 2 days to answer questions. When they sign, press Next step.",
                      "Most people need one friendly check-in before they say yes."),
    "Closed-Won": ("Collect the deposit, sign the contract, and put the job on your calendar. 🎉",
                   "Your Jobs won number goes up so you can see what's working."),
}


def build(layout):
    label = lambda s: layout["labels"].get(s, s)
    box = lambda s: f"{ICON[s]} {label(s)}"
    hidden = set(layout["hidden"])
    path = [s for s in layout["order"] if s not in hidden and s != "Nurture/Follow-Up"]

    sections = [
        {
            "title": "☀️ Every morning (5 minutes)",
            "steps": [
                ("Double-click **RemodFlow** on your desktop.", "It opens in your web browser. Nothing to log in to."),
                ("Click **🔔 To-Do**. If there's a number on it, send everything on that page.",
                 "Those are check-in texts and monthly emails that are due today."),
                (f"Click **🏠 My Leads** and look at the {box('Pre-Qualified')} box. Text every card that says **Text them now!**",
                 "These are your best customers. Speed wins the job."),
            ],
        },
        {
            "title": "📥 When a new lead comes in",
            "steps": [
                ("Open the Facebook lead email (or text) and copy all of it.", "You don't have to clean it up. Messy is fine."),
                ("Click **➕ Add a Lead**, paste it in the big box, and press **✨ Add this lead**.",
                 "Remod Flow reads it and drops the customer in the right box by itself."),
                (f"If they landed in {box('Pre-Qualified')}: press **📋 Copy** under First text, send it from your phone, "
                 "then press **✅ I sent it**. Try to do this within 5 minutes.",
                 "Pressing I sent it starts a 4-day timer for the check-in text."),
                (f"If they landed in {box('New Lead')}: call or text them to get the missing answers, then fix their info and press **💾 Save and re-sort**.",
                 "The green box tells you exactly what's missing."),
                (f"If they landed in {box('Nurture/Follow-Up')}: do nothing now.",
                 "They aren't ready yet. Their monthly emails will show up on your To-Do page."),
            ],
        },
        {
            "title": "🚚 Moving a customer toward a signed job",
            "intro": "Your boxes go in this order. Open a customer, do the step, then press the green **Next step** button.",
            "steps": [(f"**{box(s)}:** {BOX_STEPS[s][0]}", BOX_STEPS[s][1]) for s in path if s in BOX_STEPS],
        },
        {
            "title": "⏰ When someone doesn't answer",
            "steps": [
                ("4 days after the First text, the ⏰ Check-in text shows up on **🔔 To-Do**. Copy it, send it, press **✅ I sent it**.",
                 "A short, friendly nudge often brings back quiet customers."),
                ("If they answer and book a call, press **📅 They booked a call**.",
                 f"That moves them to {box('Discovery Call Scheduled')} for you."),
                (f"If they say \"not this year\", open them and press **🔄 Not ready yet**.",
                 "They go on the monthly email list so you stay in touch without extra work."),
            ],
        },
        {
            "title": "📧 Once a month",
            "steps": [
                ("On **🔔 To-Do**, open each email that says **send today**. Copy the subject and the email, send it, press **✅ I sent it**.",
                 "3 helpful emails keep you top of mind until they're ready."),
                ("The first email has blanks like $[amount]. Fill in your real prices before sending.",
                 "Real numbers build trust."),
                (f"When a not-ready customer says they're ready, open them and press **They're ready now ➡**.",
                 f"That moves them to {box('Pre-Qualified')} so you can text them."),
            ],
        },
        {
            "title": "📈 Every Friday: get more leads (10 minutes)",
            "steps": [
                ("Look at **Possible money** and **Jobs won** at the top of My Leads.", "It shows if your ads are paying off."),
                ("Open every card that hasn't moved this week and do its next step, or press **🔄 Not ready yet**.",
                 "Stuck cards are lost money. Keep the board clean."),
                ("Keep your Facebook lead ad running in a 15 to 25 mile circle around your area. See **📘 Setup Help**.",
                 "More ads in the right area means more leads to add."),
                ("Keep the 3 questions on your Facebook form: owns the home, budget, start date.",
                 "Those answers let Remod Flow sort every lead for you."),
                ("When you're ready, set up GoHighLevel using **📘 Setup Help** so the First text goes out by itself.",
                 "Then new leads get a text in 1 minute, even while you're on a job site."),
            ],
        },
        {
            "title": "🎬 Learning with training videos",
            "steps": [
                ("Click **🎬 Training Videos** in the top menu and press play on any video.",
                 "Watch a video before you try something new. If a video has a 📄 Printable steps button, print it and follow along."),
                ("To keep a new video, press **➕ Add a video**, pick it from your computer, and give it a name.",
                 "Record yourself doing a task once, and a helper can learn it without you."),
                ("Use **✏️ Rename** to change a video's name or description, and **🗑 Remove** to delete one.",
                 "Clear names make the right video easy to find."),
            ],
        },
        {
            "title": "🧭 Flow Map and page bots",
            "intro": "The Flow Map draws your whole app as boxes, built by reading the app's own code. Bots only ask: nothing changes until you press Approve.",
            "steps": [
                ("Click **🧭 Flow Map**. Each box is a page. It shows what the page reads, what it saves, which pages it feeds, "
                 "its schedules and any outside services. The dot shows how the page is doing right now.",
                 "Green is good, yellow needs you, red means something is wrong, black means the hard stop is on."),
                ("Drag a box by its top bar to move it. The lines move around the boxes by themselves.",
                 "Boxes need a little space between them. If you drop one too close, it goes back."),
                ("Click a box to see its details and its 🤖 bot. Turn on only the switches you want that bot to have.",
                 "Every bot starts with all switches off. Each switch you flip is saved as a new version."),
                ("Press **➕ New flow** and fill in the boxes left to right: **⚡ When…**, **❓ Only if…**, **▶ Then ask to…**. "
                 "Press **💾 Save as a new version**, then **✅ Approve** it.",
                 "A flow stays off until you approve it. If you edit it, you approve the new version again."),
                ("When a flow wants to do something, a request shows at the top of the Flow Map. Press **✅ Approve** to do it or **✖ No** to skip it.",
                 "This is the only way a bot ever changes a customer."),
                ("Something looks wrong? Press the red **⛔ HARD STOP**.",
                 "Every bot freezes at once and anything waiting is cancelled. A red Bots stopped tag shows at the top of every page until you turn it off."),
                ("Press **📜 Change log** to see every change, newest first. Click **versions** to go back to an earlier version.",
                 "Going back is saved as a new version, so nothing is ever lost."),
            ],
        },
        {
            "title": "🎨 Make it yours",
            "steps": [
                ("On **🏠 My Leads**, press **🎨 Customize boxes** to move, rename or hide boxes. Press **✔ Done** when finished.",
                 "This guide updates itself to match your box names and order."),
                ("Not sure what a button does? Point your mouse at it (or tap it) to see a tip.", "Every button has one."),
            ],
        },
    ]
    return sections


def to_markdown(layout):
    lines = ["# Remod Flow: How to Use", "",
             f"_Updated {date.today().strftime('%B %d, %Y')}. This guide updates itself when you change your boxes._", ""]
    n = 1
    for sec in build(layout):
        lines += [f"## {sec['title']}", ""]
        if sec.get("intro"):
            lines += [sec["intro"], ""]
        for do, tip in sec["steps"]:
            lines += [f"{n}. {do}", f"   - 💡 {tip}"]
            n += 1
        lines.append("")
    return "\n".join(lines)


def save_markdown(layout):
    try:
        with open(MD_PATH, "w", encoding="utf-8") as f:
            f.write(to_markdown(layout))
    except OSError:
        pass
