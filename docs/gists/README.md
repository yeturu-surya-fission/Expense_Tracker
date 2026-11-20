# Code gists

Small, self-contained snippets from the blogs, ready to publish as GitHub Gists.

| File                               | Idea                                           |
| ---------------------------------- | ---------------------------------------------- |
| `01_architecture_boundary_test.py` | Architecture rules that fail CI                |
| `02_pooled_policy_caps.py`         | The model reads the policy, code does the sums |
| `03_signed_webhook.py`             | HMAC-signed, retried, idempotent decisions     |

Publish one (secret by default; add `--public` to share):

```powershell
gh gist create docs/gists/01_architecture_boundary_test.py -d "Architecture rules that fail CI"
```

Then paste the gist URL into the blogs and the README table below.
