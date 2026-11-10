-- "VAT Bot Dashboard" — Desktop icon.
-- Starts the local status server if it is not already running, waits
-- for it to answer, then opens the page in the default browser.
-- Safe to double-click repeatedly: it never starts a second server.

set projectDir to "/Users/tashkent/Tashkent/vat-bot"
set dashURL to "http://127.0.0.1:8808/"

-- Already serving? Then just open it.
set isUp to false
try
	do shell script "/usr/bin/curl -s -o /dev/null --max-time 2 " & quoted form of dashURL
	set isUp to true
end try

if not isUp then
	try
		-- Start it fully detached. AppleScript's "do shell script" waits
		-- for every inherited file descriptor to close, so a plain
		-- "... &" hangs forever on a long-running server. Wrapping in a
		-- subshell AND redirecting stdin/stdout/stderr of the outer
		-- command is what lets this return immediately.
		do shell script "cd " & quoted form of projectDir & " && " & ¬
			"( ./.venv/bin/python scripts/dashboard.py " & ¬
			"> /tmp/vatbot_dashboard.log 2>&1 & ) " & ¬
			"< /dev/null > /dev/null 2>&1"
	on error errMsg
		display alert "Could not start the dashboard" message errMsg as critical buttons {"OK"} default button "OK"
		return
	end try

	-- Wait up to ~15s for it to answer.
	repeat 15 times
		delay 1
		try
			do shell script "/usr/bin/curl -s -o /dev/null --max-time 2 " & quoted form of dashURL
			set isUp to true
			exit repeat
		end try
	end repeat
end if

if isUp then
	open location dashURL
else
	display alert "The dashboard did not start" message "Open Terminal and run:" & return & return & "cd " & projectDir & return & "./.venv/bin/python scripts/dashboard.py" as critical buttons {"OK"} default button "OK"
end if
