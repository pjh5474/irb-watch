"""The assistant: a chat that answers from the documents a user may read.

Read-only by design. Every tool runs for one user and checks that user's access on the server;
no tool writes anything, sends mail or reaches the network. Admins can have the assistant
*propose* an action, which does nothing until the admin clicks Confirm in the browser.
"""
