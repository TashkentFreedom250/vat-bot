-- "Restore VAT Bot" — Desktop button.
-- Thin wrapper: all real logic lives in scripts/vatbot_restore.sh so
-- the behaviour stays version-controlled in the repo.

set scriptPath to "/Users/tashkent/Tashkent/vat-bot/scripts/vatbot_restore.sh"

display notification "Starting MongoDB and the bot..." with title "VAT Bot" subtitle "Restoring"

set theOutput to ""
set failed to false
try
	set theOutput to do shell script "/bin/bash " & quoted form of scriptPath & " 2>&1"
on error errMsg
	set theOutput to errMsg
	set failed to true
end try

if failed then
	display notification "Restore failed - see details." with title "VAT Bot" subtitle "Still down"
	display alert "VAT Bot could NOT be restored" message theOutput & return & return & "If it mentions the internet, check the network first, then press the button again." as critical buttons {"OK"} default button "OK"
else
	display notification "Users can send receipts again." with title "VAT Bot" subtitle "Back online"
	display alert "VAT Bot is back online" message theOutput as informational buttons {"Done"} default button "Done"
end if
