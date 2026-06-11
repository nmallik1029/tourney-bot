# test_webhook.ps1
# Simulate a Krunker "match_end" POST to your LOCAL bot to verify the result flow.
#
# HOW TO USE:
#   1. Start the test bot (run_test.bat).
#   2. In Discord, get a PUG match to the "Click to Host" stage (phase = host).
#      (Force-pop 2 linked accounts for a quick 1v1, or run a full 8.)
#   3. Fill in the two players' LINKED usernames below (exactly as set via /link).
#   4. In a NEW PowerShell window (not the one running the bot), run:
#         powershell -ExecutionPolicy Bypass -File .\test_webhook.ps1
#   5. Watch #results (a scoreboard + ELO embed should post), the match channel
#      (a "won the match" embed + 10s teardown), and the bot terminal for a
#      line starting with [Pug] match_end.
#
# Matching is by PLAYER IDENTITY, so the team "name" values don't have to be
# right — only the players[].name must equal the linked usernames.

# ---- EDIT THESE ----
$player1 = "ilIegaI"   # a player on team 1
$player2 = "afghan1239"   # a player on team 2
$winner  = 1                             # 1 = team 1 wins, 2 = team 2 wins
$map     = "Sandstorm"
$botUrl  = "http://localhost:5000/krunker"
# --------------------

$body = @{
  type   = "match_end"
  map    = $map
  winner = $winner
  teams  = @(
    @{ team = 1; name = $player1; score = 6 },
    @{ team = 2; name = $player2; score = 3 }
  )
  players = @(
    @{ team = 1; name = $player1; score = 1335; kills = 12; deaths = 4; objective_score = 1260; damage_done = 600 },
    @{ team = 2; name = $player2; score = 600;  kills = 4;  deaths = 12; objective_score = 60;  damage_done = 300 }
  )
} | ConvertTo-Json -Depth 6

Write-Host "POSTing match_end to $botUrl ..."
try {
    $resp = Invoke-RestMethod -Uri $botUrl -Method Post -Body $body -ContentType "application/json"
    Write-Host "Bot responded: $resp"
    Write-Host "Now check #results, the match channel, and the bot terminal for [Pug] match_end."
} catch {
    Write-Host "Request failed: $($_.Exception.Message)"
    Write-Host "Is the bot running and is the webhook server on port 5000?"
}
