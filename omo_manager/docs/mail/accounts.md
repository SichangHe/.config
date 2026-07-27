# email accounts

The agent Gmail account is the communication mailbox. `email_me.py` sends from it only to `OMO_HUMAN_EMAIL_ADDRESS`; `email_idle_watcher.py` reads its inbox and accepts mail only when the visible sender, Gmail transport sender, and Gmail SPF result all match that exact human address. Subjects need no manager tag. Existing `[a] [TARGET]` tags remain routing metadata when a reply retains them.

Configure these three values together in `~/.config/omo_manager/local.env`:

```sh
export OMO_AGENT_GMAIL_ADDRESS="agent-account@gmail.com"
export OMO_AGENT_GMAIL_APP_PASSWORD="agent-app-password"
export OMO_HUMAN_EMAIL_ADDRESS="human-account@gmail.com"
```

`OMO_HUMAN_EMAIL_CONFIG_PATH` is optional and defaults to `~/.config/himalaya/config.toml`. It remains the separate Himalaya configuration for cleanup of the human mailbox. Cleanup accepts only `[a]` mail sent from the configured agent address to the configured human address.

All three agent/human address and credential values are atomic: partial configuration is rejected, and the two addresses must differ. Until none of them are set, the legacy self-addressed configuration remains active so migration does not stop mail delivery.
