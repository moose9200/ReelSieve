"""The host message: drafted here, sent by the customer from their own Airbnb inbox.

ReelSieve never drives an Airbnb session of its own. The job page copies the message and opens
the host's contact form in the customer's browser; they review it and press Send.
Form verified 19 Sep 2026 on /contact_host/<id>/send_message.
"""
DEFAULT_MESSAGE = ("Hi {host_name}! I run ReelSieve (by Braivex) — we turn a listing's own photos and reviews into a short cinematic video, and I made one for \"{listing_title}\" as a free sample.\n\n"
                   "Airbnb doesn't let me send links here, so to watch it just search YouTube for: {search_phrase}\n\n"
                   "If you'd like the full-resolution file, a version for Instagram, or one for your other properties, tell me where to send it and it's yours.")
# tokens: {host_name} {listing_title} {city} {search_phrase} {reel_link} — the default is link-free (Airbnb filters links before a booking)


def contact_url(listing_id):
    return f'https://www.airbnb.co.uk/contact_host/{listing_id}/send_message'
