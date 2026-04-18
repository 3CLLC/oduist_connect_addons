# Changelog

All notable changes to the Connect module are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [1.0.5] - 2026-04-17

### Fixed

- `reload_view` bus handler now skips form views, preventing unsaved user edits
  on `res.partner` (and any other model) from being wiped when calls receive a
  summary, or when inbound/outbound SMS messages are processed. Chatter updates
  continue to arrive via the mail bus; stat buttons refresh on next navigation.
  List and kanban views retain the existing reload behaviour.
