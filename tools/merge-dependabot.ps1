while ($true) {
    $prs = gh pr list --author "app/dependabot" --state open --json number |
        ConvertFrom-Json

    if (-not $prs) { break }

    foreach ($pr in $prs.number) {
        Write-Host "Attempting PR #$pr"

        gh pr merge $pr --auto --merge 2>$null

        if ($LASTEXITCODE -ne 0) {
            Write-Host "Could not merge #$pr, skipping for now"
        }
    }

    Start-Sleep -Seconds 120
}