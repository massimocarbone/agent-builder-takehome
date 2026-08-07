"""Query set for the KB_RETRIEVAL_MODE shadow-mode evaluation (DECISIONS.md §8A).

Not imported by src/ or collected by pytest — a data file for the one-off comprehensive
testing pass. Stratified per the plan: typos, terse, multi-intent, zero-overlap paraphrase,
known homonym traps, and genuinely uncovered topics, plus category-representative queries
so every article family gets exercised at least once.
"""

QUERIES = [
    # --- Straightforward, one per category (sanity baseline) ---
    "how do I extend my rental",
    "what happens if I return the car late",
    "can I change my pickup time",
    "how do I cancel my reservation",
    "how do I upgrade to Avis Preferred",
    "who is allowed to extend or modify a rental",
    "what are the Avis Preferred membership benefits",
    "can I talk to a real person",
    "are there fees for returning outside business hours",
    "what is a one-way fee",

    # --- Known homonym / hard traps (DECISIONS.md §2) ---
    "what is the grace period for late returns",
    "what is the grace period",
    "do you cover insurance or a damage waiver",
    "is there a pet policy",
    "what's your policy on pets",
    "what is your policy",  # meta-term stopword test

    # --- Typos ---
    "how do i extned my rentl",
    "cancelation policy",
    "wat happens if i retrun late",
    "chaning my pickup time",
    "prefered member benifits",
    "how much is teh late fee",

    # --- Terse ---
    "extend?",
    "late fee",
    "cancel",
    "upgrade",
    "refund",
    "grace period",
    "one way fee",
    "child seat",
    "pet fee",

    # --- Multi-intent ---
    "can I extend and also change the pickup location",
    "I want to cancel but also know if I get a refund",
    "how do I upgrade and will that waive my late fee",
    "can I change my return time and location at once",
    "if I extend will my rate change and is there a fee",

    # --- Paraphrase, zero/near-zero lexical overlap ---
    "can I keep the car a couple more days",
    "I'm not going to make it back in time, what happens",
    "I don't need the vehicle anymore, what do I do",
    "can somebody else pick up the car for me",
    "will it cost extra to drop off somewhere else",
    "does my membership tier get me anything special",
    "what if nobody shows up to get the car",
    "how do refunds work when plans change",
    "is my card required again if I change dates",
    "can I get a different kind of car after booking",

    # --- Genuinely uncovered topics (no_coverage expected) ---
    "what is your pet policy",
    "do you offer roadside assistance",
    "can I add a child seat",
    "is smoking allowed in the car",
    "do you have fuel discounts",
    "will you cover my tolls",
    "what happens if I get a speeding ticket",
    "can I book a new reservation through you",
    "how much does a new rental cost",
    "do you sell travel insurance",
    "what is your policy on driving into Mexico",
    "can I rent if I'm under 21",
    "do you offer GPS rental",
    "is there a shuttle service",
    "can I add an additional driver",

    # --- Category: extensions (varied phrasing) ---
    "how are extension charges calculated",
    "can I extend for just one extra day",
    "will extending change my daily rate",
    "is the car guaranteed to still be available if I extend",
    "what's the difference between extending and returning late",
    "if I'm late does that count as an extension",
    "I need the car for one more day only",
    "extending my rental by a week, is that possible",

    # --- Category: fees ---
    "how much is the late return fee",
    "what taxes and surcharges apply",
    "is there a fee for changing locations",
    "what if I return the car with less fuel than I got it",
    "returning after hours, what happens",
    "why was I charged extra for fuel",

    # --- Category: modifications ---
    "can I change where I drop off the car",
    "can I change my pickup time to earlier",
    "I booked a sedan can I switch to an SUV",
    "the car I wanted isn't available, what happens",
    "how far in advance can I change my reservation",

    # --- Category: cancellations ---
    "will I get charged if I cancel",
    "what's the refund timeline after cancelling",
    "I never picked up the car, am I charged",
    "can I return the car early without cancelling",
    "if I bring the car back early do I get money back",
    "does cancelling within 24 hours cost more",
    "I want to end my rental early, is that a cancellation",

    # --- Category: upgrades ---
    "how do I become an Avis Preferred member",
    "what do I get with an upgrade",
    "can I upgrade my vehicle class after booking",
    "does upgrading cost extra",
    "what are the eligibility requirements for Preferred",

    # --- Category: eligibility / payment ---
    "does the person modifying the rental need to be the driver",
    "what payment info do you need to make a change",
    "can I use a debit card",
    "do you need my CVV to extend",
    "what's required to change my reservation",

    # --- Category: preferred program ---
    "how do I keep my Preferred status",
    "does Preferred waive late fees",
    "what if I'm a few minutes late as a Preferred member",
    "how much flexibility do Preferred members get on timing",

    # --- Escalation / representative ---
    "I want to speak to a manager",
    "this isn't working, connect me to someone",
    "my situation is complicated, can a human help",

    # --- Adjacent, plausible-but-outside servicing scope ---
    "how do I book a new rental",
    "what cars do you have available",
    "what's your cheapest rate",
    "do you have locations near the airport",
    "can I rent a car for a road trip to another country",

    # --- Ambiguous / compound phrasing stress ---
    "late fee policy for rentals extended past original return",  # REVIEW_QUEUE #8
    "extension charges for late returns beyond original date",
    "policy for extending a rental that is already late",
    "what happens to my extension if I'm already past due",

    # --- Short single-token or near-empty ---
    "fees",
    "policy",
    "help",
    "rules",
    "membership",

    # --- Longer, natural customer phrasing ---
    "hey so I picked up my car last week and I think I need a few more days, what do I need to do and will it cost a lot more",
    "I'm supposed to return today but flights got cancelled and I won't make it, is there a fee for that",
    "quick question, if I extend twice in one rental does that cause any issues",
    "trying to figure out if it's cheaper to extend now or just pay the late fee when I bring it back",
    "we decided to end the trip early and don't want the car anymore, do we owe anything",
]

assert len(QUERIES) >= 150, f"only {len(QUERIES)} queries, need >=150"
