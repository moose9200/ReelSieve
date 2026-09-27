"""The host message: drafted here, sent by the customer from their own Airbnb inbox.

ReelSieve never drives an Airbnb session of its own. The job page copies the message and opens
the host's contact form in the customer's browser; they review it and press Send.
Form verified 19 Sep 2026 on /contact_host/<id>/send_message.
"""
DEFAULT_MESSAGE = ("Hi {host_name}! I run ReelSieve (by Braivex) — we turn a listing's own photos and reviews into a short cinematic video, and I made one for \"{listing_title}\" as a free sample.\n\n"
                   "If you'd like to see it, just reply and I'll send it to you privately.\n\n"
                   "I can also make a version for Instagram, or for your other properties. If you'd rather not hear from me, say so and I won't message again.")
# tokens: {host_name} {listing_title} {city} {search_phrase} {reel_link}. The default is link-free (Airbnb filters links
# before a booking) and never asks the host to look the reel up anywhere public: it is someone else's listing.


def contact_url(listing_id):
    return f'https://www.airbnb.co.uk/contact_host/{listing_id}/send_message'
