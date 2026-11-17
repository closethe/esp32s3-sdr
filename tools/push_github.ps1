<#
Push this fork to your own GitHub repository.

  .\tools\push_github.ps1 -RepoUrl https://github.com/<you>/esp-sdr   # HTTPS, opens a browser login
  .\tools\push_github.ps1 -RepoUrl git@github.com:<you>/esp-sdr.git   # SSH, needs a key on your account
  .\tools\push_github.ps1 -RepoUrl https://github.com/<you>/esp-sdr -Token $env:GH_TOKEN

The script never force-pushes. It adds (or updates) a remote named `mine`,
checks that GitHub answers, pushes `main` and then reads the remote ref back to
prove the push landed.
#>
param(
    [Parameter(Mandatory = $true)][string]$RepoUrl,
    [string]$RemoteName = "mine",
    [string]$Branch = "main",
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"

function ConvertToSsh([string]$url) {
    if ($url -match '^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$') {
        return "git@github.com:$($Matches[1])/$($Matches[2]).git"
    }
    return $url
}

$root = (& git rev-parse --show-toplevel) 2>$null
if (-not $root) { throw "run this inside the esp-sdr git working tree" }
Set-Location -LiteralPath $root.Trim()
Write-Host "repo: $root"

if ($RepoUrl -match '^https://github\.com/espressif/') {
    throw "that is the upstream project; create your own fork on GitHub first"
}

# Local URL rewrites can silently redirect a github.com push to a mirror.
$rewrites = (& git config --get-regexp "^url\.") 2>$null
if ($rewrites) { Write-Host "active url rewrites:`n$rewrites" }
if ($rewrites -match 'insteadof https://github\.com/ ') {
    throw "remove the broad insteadOf rewrite first, it would redirect your push to a mirror"
}

$dirty = (& git status --porcelain)
if ($dirty) {
    Write-Warning "working tree is not clean, only commits will be pushed:"
    Write-Host $dirty
}

# Unborn history cannot be pushed.
$head = (& git rev-parse --verify $Branch) 2>$null
if (-not $head) { throw "branch '$Branch' does not exist yet, commit something first" }

& git remote get-url $RemoteName 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    & git remote set-url $RemoteName $RepoUrl
    Write-Host "remote $RemoteName -> $RepoUrl (updated)"
} else {
    & git remote add $RemoteName $RepoUrl
    Write-Host "remote $RemoteName -> $RepoUrl (added)"
}

# Reachability check, so a bad URL or blocked network fails before the push.
if ($RepoUrl -match '^git@github\.com:') {
    $probe = (& ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1)
    if ($probe -notmatch 'successfully authenticated') {
        throw "GitHub rejected the SSH key. Add ~/.ssh/id_ed25519.pub to Settings > SSH and GPG keys. Output: $probe"
    }
    Write-Host "ssh: $($probe.Trim())"
} else {
    if ($Token) {
        # Do not persist the token; hand it to git for this one invocation only.
        $clean = $RepoUrl -replace '^https://', "https://x-access-token:$Token@"
        & git -c credential.helper= push -u $clean "HEAD:$Branch" 2>&1 | ForEach-Object { Write-Host $_ }
    } else {
        & git push -u $RemoteName "HEAD:$Branch" 2>&1 | ForEach-Object { Write-Host $_ }
    }
}
if ($LASTEXITCODE -ne 0) { throw "push failed (exit $LASTEXITCODE)" }

$local = (& git rev-parse HEAD).Trim()
$remoteHead = $RepoUrl
if ($Token) { $remoteHead = $RepoUrl }
$remoteRef = (& git -c "credential.helper=" ls-remote $remoteHead "refs/heads/$Branch" 2>$null | Select-Object -First 1)
if (-not $remoteRef) { throw "push reported success but the remote ref is missing" }
$sha = ($remoteRef -split "`t")[0]
if ($sha -ne $local) {
    throw "remote $Branch is $sha, local is $local - not the same commit"
}
Write-Host "ok: $Branch = $sha on $RepoUrl"
Write-Host "next: set git config user.email to your GitHub address so commits link to your account"
