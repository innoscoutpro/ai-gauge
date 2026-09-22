"""Provider verification targets.

Kept free of any QtWebEngine import so callers can ask *whether* a provider
has a verification target without pulling Chromium — and its GL requirement —
into the process (issue #7).
"""

from __future__ import annotations

# Load the provider's actual usage page and check for text that only renders for
# a signed-in user. If the cookie is good the page renders inline; if not it
# either redirects to /login or shows an interstitial.
VERIFY_TARGETS = {
    "claude": (
        "https://claude.ai/new#settings/usage",
        r"""(() => {
          const text = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').trim();
          const usageVisible = /Plan usage limits|Current session|All models/i.test(text);
          const loginVisible = /Log in to Claude|Sign in to Claude|Create an account/i.test(text);
          const appShellVisible = /How can I help you today\?/i.test(text) ||
            (/\bNew\b/i.test(text) && /\bProjects\b/i.test(text) &&
              /\b(?:Artifacts|Chats and tasks)\b/i.test(text));
          const authenticatedRoute = location.pathname === '/new' ||
            location.pathname === '/settings/usage' ||
            /settings\/usage/i.test(location.hash);
          const onClaude = location.hostname === 'claude.ai';
          const claudeTitle = /(?:^|[-–—]\s*)Claude$/i.test(document.title.trim());
          const claudeShell = (appShellVisible || claudeTitle) &&
            authenticatedRoute && !/\/login(?:\/|$)/i.test(location.pathname);
          return onClaude && !loginVisible && (usageVisible || claudeShell);
        })()""",
    ),
    "codex": (
        "https://chatgpt.com/codex/cloud/settings/analytics#personal-usage",
        r"""(() => {
          const visibleText = el => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
          const text = visibleText(document.body);
          if (/Weekly usage limit/i.test(text) && /\d+(?:\.\d+)?\s*%/.test(text)) {
            return true;
          }
          const labels = Array.from(document.querySelectorAll('button,a,[role="tab"],[role="button"]'));
          const label = labels.find(el => visibleText(el).toLowerCase() === 'personal usage');
          const target = label && (label.closest('button,a,[role="tab"],[role="button"]') || label);
          if (target) target.click();
          return false;
        })()""",
    ),
}
