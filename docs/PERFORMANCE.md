# Performance

How fast the connector answers, how many network round trips each tool costs, and how much text it hands an agent. Every change
that claims to make something faster records its numbers here, measured the same way.

## How to measure

- `python dev/bench.py --local` runs agent-like scenarios against the local stack (`dev/start_local_stack.sh`: Dovecot, an SMTP
  sink and Radicale, all on 127.0.0.1). CI runs it on every pull request (job `bench` in `.github/workflows/tests.yml`) and puts
  the tables in the job summary and the `bench` artifact.
- `--latency-ms 40` adds 40 ms to every TCP connect and every request or command, which is roughly what iCloud costs from Europe.
  On localhost a round trip is almost free, so without it the timings hide exactly what the network charges for.
- `python dev/bench.py --live --env <server .env> [--scratch-calendar NAME]` measures a real account: read-only tools only, plus
  create/update/delete of one event inside the named scratch calendar. It never sends mail and never writes contacts.
- `python dev/tool_surface.py` measures the text an agent receives before doing anything: tool descriptions, parameter schemas and
  the instructions.

**Read the counts first.** Round trips (TCP connects, logins, IMAP commands, CalDAV and CardDAV requests) are what a real network
charges for, and they are exact. Local timings are noisy and small.

Known quirk of the local stack: Radicale answered each CalDAV request on its own TCP connection, so locally `tcp connects` equals
`caldav requests`. iCloud keeps connections alive, so there the connect count is lower; the request count is what carries over.

## Baseline (0.5.0, 24 September 2026)

Local stack in CI (GitHub-hosted Ubuntu runner, Python 3.12), 5 runs per scenario, fictional seed data: 60 inbox messages (a third
of them newsletters, six with a 300 KB PDF), 20 archived, 10 sent, five calendars with a month of events and a weekly series each,
200 contacts.

### local stack, 5 runs

| scenario | median s | p90 s | bytes | notice chars | tcp connects | imap logins | imap commands | caldav requests | carddav requests |
|---|---|---|---|---|---|---|---|---|---|
| mail_search 20 + get_messages 10 | 0.076 | 0.107 | 35156 | 327 | 0 | 0 | 6 | 0 | 0 |
| mail_search all_folders | 0.049 | 0.052 | 14111 | 124 | 0 | 0 | 14 | 0 | 0 |
| mail_get_attachment (300 KB pdf) | 0.041 | 0.042 | 423823 | 248 | 0 | 0 | 6 | 0 | 0 |
| calendar_list_calendars (cold) | 1.271 | 1.457 | 491 | 0 | 9 | 0 | 0 | 9 | 0 |
| calendar_list_calendars (warm) | 0.006 | 0.006 | 491 | 0 | 2 | 0 | 0 | 2 | 0 |
| calendar_list_events 7 days | 0.073 | 0.084 | 15758 | 160 | 7 | 0 | 0 | 7 | 0 |
| calendar_list_events 30 days | 0.160 | 0.161 | 26017 | 160 | 7 | 0 | 0 | 7 | 0 |
| calendar_find_free_time 14 days | 0.102 | 0.104 | 2395 | 160 | 7 | 0 | 0 | 7 | 0 |
| contacts_search (cold) | 0.118 | 0.118 | 8609 | 142 | 5 | 0 | 0 | 0 | 5 |
| contacts_search (warm) | 0.003 | 0.116 | 8609 | 142 | 0 | 0 | 0 | 0 | 0 |
| calendar create + update + delete | 0.063 | 0.069 | 1397 | 0 | 13 | 0 | 0 | 13 | 0 |
| mail_search x5, 20 s apart | 0.008 | 0.035 | 0 | 0 | 1 | 1 | 18 | 0 | 0 |

### local stack, 5 runs, +40 ms per round trip

| scenario | median s | p90 s | bytes | notice chars | tcp connects | imap commands | caldav requests | carddav requests |
|---|---|---|---|---|---|---|---|---|
| mail_search 20 + get_messages 10 | 0.319 | 0.520 | 35156 | 327 | 0 | 6 | 0 | 0 |
| mail_search all_folders | 0.622 | 0.623 | 14111 | 124 | 0 | 14 | 0 | 0 |
| mail_get_attachment (300 KB pdf) | 0.288 | 0.289 | 423823 | 248 | 0 | 6 | 0 | 0 |
| calendar_list_calendars (cold) | 1.867 | 2.237 | 491 | 0 | 9 | 0 | 9 | 0 |
| calendar_list_calendars (warm) | 0.170 | 1.538 | 491 | 0 | 2 | 0 | 2 | 0 |
| calendar_list_events 7 days | 0.652 | 0.661 | 15758 | 160 | 7 | 0 | 7 | 0 |
| calendar_list_events 30 days | 0.738 | 0.741 | 26017 | 160 | 7 | 0 | 7 | 0 |
| calendar_find_free_time 14 days | 0.681 | 0.683 | 2395 | 160 | 7 | 0 | 7 | 0 |
| contacts_search (cold) | 0.525 | 0.532 | 8609 | 142 | 5 | 0 | 0 | 5 |
| contacts_search (warm) | 0.003 | 0.524 | 8609 | 142 | 0 | 0 | 0 | 0 |
| calendar create + update + delete | 1.127 | 2.568 | 1397 | 0 | 13 | 0 | 13 | 0 |

### Tool surface (every area on)

| tool | description chars | schema chars |
|---|---|---|
| calendar_create_event | 407 | 3953 |
| calendar_update_event | 331 | 3410 |
| mail_reply | 689 | 2515 |
| contacts_update | 241 | 2961 |
| contacts_create | 224 | 2617 |
| mail_bulk_action | 550 | 1907 |
| mail_send | 688 | 1684 |
| mail_search | 379 | 1969 |
| mail_forward | 398 | 1805 |
| calendar_find_free_time | 468 | 1726 |
| contacts_search | 738 | 638 |
| mail_get_messages | 407 | 912 |
| mail_extract_bookings | 594 | 705 |
| reminders_create | 186 | 1087 |
| drive_search_content | 464 | 803 |

**65 tools**: 18,006 description chars, 51,951 schema chars; instructions 4,331 chars.

### Live reference (22 September 2026, before this work)

Measured by hand against iCloud from the Netherlands: `calendar_list_calendars` 0.64 s, `calendar_list_events` a week 2.6 s and a
month 3.1 s, `calendar_get_event` 1.3 s, create 1.2 s, update 1.8 s, delete 1.8 s; `contacts_search` 1.7 s cold and 2 ms warm; an
IMAP login about 1 s.
