"""What Jev is asked. Editing this file changes the questions; nothing in the database
restricts the values, so categories and wording can be changed later without a migration.
Old rows keep the values they were given.
"""

PRIORITY_QUESTION = "How important is this change for clinical research coordinators at study sites?"

PRIORITY_CRITERIA = {
    "high": (
        "The change alters a form, SOP or rule that must be followed or submitted, such as a "
        "consent form, an application form, a review timeline or safety reporting requirements."
    ),
    "medium": (
        "New or revised guidance, notices, reference documents or explanations that people "
        "should know about but that do not change what must be submitted."
    ),
    "low": (
        "A title-only edit, re-formatting, a corrected typo, or a new copy of content that is "
        "otherwise unchanged."
    ),
}

SUBSTANTIVE_STATEMENT = (
    "The change alters what a person must do, submit or follow: a requirement, deadline, "
    "form field, procedure, rule or definition."
)

CATEGORY_QUESTION = "What kind of document is this?"

CATEGORY_CRITERIA = {
    "consent_form": "Informed consent templates and guidance on writing consent documents.",
    "application_form": "Forms used to submit, amend, renew or close a study at the IRB.",
    "sop": "Standard operating procedures of the IRB or the hospital.",
    "regulation_guideline": "Laws, regulations and official guidelines.",
    "notice": "Announcements, circulars and schedule or policy notices.",
    "reference": "Education material, leaflets, videos and general reference documents.",
    "other": "Anything that fits none of the other kinds.",
}
