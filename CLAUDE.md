# Working in this repo

## Leave NOTES.md alone

`NOTES.md` is the project's long-form write-up -- motivation, hardware
measurements, benchmark results, the reasoning behind choices already made.
It is hand-written prose, kept by hand, and it is not a specification.

Do not read it, and do not edit it, unless I ask for it by name.

Two reasons:

1. **It is 110 KB of narrative.** Reading it spends a large part of the context
   window on background that almost never changes what the code should do. The
   answer to "why is it like this" is in the module headers, which sit next to
   the code they explain and stay true when the code moves.
2. **It is mine to write.** It records what was measured on a particular box on
   a particular day. Rewriting a paragraph of it to match a change you just made
   turns a record into a guess, and I have no way to tell the two apart later.

If a change makes something in NOTES.md wrong -- a tunable renamed, a number no
longer reproducible -- say so in your summary and let me decide. Do not fix it
in passing.

The same goes for reading it "just to check": if you need a fact from it, ask
for the fact, or grep for the one line rather than pulling in the file.
