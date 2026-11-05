-- Send one email with an attachment through Apple Mail (macOS), from an account already signed in to Mail.
-- usage: osascript tools/applemail_send.applescript "<to1,to2>" "<subject>" <body.txt> <attachment.pdf> "<Name <sender@address>>"
-- Prints "true" when Mail accepted the message. Check Mail's Sent mailbox ("[Gmail]/Sent Mail" for Gmail accounts).
-- Space sends 45-90 s apart (see docs/HANDOFF-2026-11-05.md) and log each one in state/outreach_sent.json.
on run argv
	set toList to item 1 of argv
	set subj to item 2 of argv
	set bodyText to read (POSIX file (item 3 of argv)) as «class utf8»
	set attPath to item 4 of argv
	set senderAddr to item 5 of argv
	set AppleScript's text item delimiters to ","
	set addrs to text items of toList
	set AppleScript's text item delimiters to ""
	with timeout of 300 seconds
		tell application "Mail"
			set m to make new outgoing message with properties {subject:subj, content:bodyText, visible:false}
			set sender of m to senderAddr
			tell m
				repeat with a in addrs
					make new to recipient at end of to recipients with properties {address:a}
				end repeat
				make new attachment with properties {file name:(POSIX file attPath) as alias} at after the last paragraph
			end tell
			delay 3
			send m
		end tell
	end timeout
	return true
end run
