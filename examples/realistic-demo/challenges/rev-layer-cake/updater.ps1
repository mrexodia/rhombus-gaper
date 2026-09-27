# Recovered scheduled-task payload. Analyze; do not execute.
$TestFixture = "rhombus{powershell_logging_test}"
$Layer = "Xk1MSldTWlFATUZ8V0xNfFBKfE1MSldCQFBWRUFMWFBWQU5MS1E="
$Bytes = [Convert]::FromBase64String($Layer)
[Array]::Reverse($Bytes)
$Output = foreach ($Value in $Bytes) { [char]($Value -bxor 0x23) }
$OperatorToken = -join $Output
if ($env:ACME_DEBUG -eq "print") { Write-Output $OperatorToken }
