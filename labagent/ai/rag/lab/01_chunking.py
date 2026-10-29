# %% [markdown]
# # 1. Chunking: cut documents into pieces you can search
#
# **Time:** about 20 minutes. **Cost:** nothing. **You need:** nothing but Python.
#
# ## The plan for the whole lab
#
# A RAG system answers a question from your own documents. Over eight notebooks you build every part of one, by hand:
#
# | # | You build | The question it answers |
# |---|---|---|
# | 1 | a chunker | How do I cut a document so a search can find the right part? |
# | 2 | similarity search with embeddings | How does a computer measure "means the same thing"? |
# | 3 | a vector store | What does a vector database actually do? |
# | 4 | the prompt and the answer | How does the model use what was found? |
# | 5 | a real vector database (Chroma) and metadata filters | What does a library add to what I wrote? |
# | 6 | hybrid search | Why does exact wording sometimes beat meaning? |
# | 7 | an evaluation | How do I know a change made it better? |
# | 8 | failure modes | Wrong, old and hostile documents |
#
# ## How the exercises work
#
# Cells marked **Your turn** contain a function with a `TODO`. Write the function, run the cell, then run the **check** cell
# below it. A check prints `ok` for each thing it verifies, or stops with a hint. If you are stuck, read the hint, then the
# docstring, and then ask. Later notebooks start from a working version of the earlier exercises, so being stuck on one
# does not block you.

# %%
from pathlib import Path
from labagent.ai.rag.labkit import check, load_docs

docs = load_docs()
for name, text in docs.items():
    print(f"{name:26} {len(text):5} characters")

# %% [markdown]
# These are eight short company documents (the policies of an invented company, Acme Corp). Open one and read it before you
# go on: `data/sample_docs/security_policy.md`.
#
# ## Why chunk at all
#
# Three reasons, and they pull in different directions:
#
# 1. **A search returns pieces, not files.** If the unit is a whole document, a question about passwords returns the entire
#    security policy and the model has to dig the answer out of it.
# 2. **An embedding is one vector per piece of text.** One vector for a whole page averages many topics into a blur. A
#    short piece with one topic gives a sharper vector.
# 3. **Every retrieved character goes into the prompt, and you pay for it.** Smaller pieces mean a smaller, cheaper prompt.
#
# The cost of going small is that a piece may lose the context that makes it make sense ("It must be rotated every 90 days":
# what must?). Chunking is a trade between precision and context. You will measure that trade in notebook 7.

# %% [markdown]
# ## Exercise 1: the simplest chunker
#
# Cut the text every `size` characters, but start each new piece `overlap` characters before the previous piece ended, so a
# sentence cut at the boundary appears whole in at least one piece.
#
# With `size=10` and `overlap=3` the text `abcdefghijklmnopqrstuvwxyz` becomes `abcdefghij`, `hijklmnopq`, `opqrstuvwx`, `vwxyz`.
# Each piece starts 7 characters (`size - overlap`) after the previous one.

# %%
def chunk_fixed(text, size=400, overlap=50):
    """Return a list of strings. Each is at most `size` characters. Piece n+1 starts `size - overlap` after piece n starts.
    Stop as soon as a piece reaches the end of the text, and never return an empty piece."""
    #### SOLUTION chunk_fixed
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    chunks, step = [], size - overlap
    for start in range(0, len(text), step):
        piece = text[start:start + size]
        if piece.strip():
            chunks.append(piece)
        if start + size >= len(text):
            break
    return chunks
    #### STUDENT
    # TODO: a loop over start positions 0, step, 2*step, ... where step = size - overlap
    # TODO: take text[start:start + size]; stop after the piece that reaches the end
    return []
    #### END

# %% [markdown]
# **Check 1.** Run this after you write `chunk_fixed`.

# %%
alpha = "abcdefghijklmnopqrstuvwxyz"
check("the example from the text", chunk_fixed(alpha, 10, 3) == ["abcdefghij", "hijklmnopq", "opqrstuvwx", "vwxyz"],
      f"expected 4 pieces starting abcdefghij, hijklmnopq... got {chunk_fixed(alpha, 10, 3)}")
long_text = docs["security_policy"]
pieces = chunk_fixed(long_text, 200, 40)
check("no piece is longer than size", all(len(p) <= 200 for p in pieces), "slice with text[start:start + size]")
check("pieces overlap by 40 characters", all(a[-40:] == b[:40] for a, b in zip(pieces, pieces[1:-1])), "the next start is start + size - overlap")
check("the last piece reaches the end of the text", pieces[-1] == long_text[-len(pieces[-1]):], "do not stop before the end of the text")
check("short text stays in one piece", chunk_fixed("hello", 400, 50) == ["hello"], "if the text is shorter than size, return it whole")

# %% [markdown]
# ### Look at what it did
#
# Print the first four pieces at `size=200`. Do not skim: read where each piece begins and ends.

# %%
for i, p in enumerate(chunk_fixed(docs["security_policy"], 200, 40)[:4]):
    print(f"--- piece {i} ({len(p)} chars)")
    print(p)

# %% [markdown]
# **Your turn (write your answer in this cell):** what is wrong with where these pieces start and end? What would a person
# reading piece 2 on its own misunderstand?
#
# *Your answer:*

# %% [markdown]
# ## Exercise 2: respect the structure
#
# Documents have a structure: headings, then paragraphs. A better chunker packs **whole paragraphs** into a piece until the
# next one would not fit. Write `chunk_paragraphs(text, size, overlap)`:
#
# 1. Split the text on blank lines (`"\n\n"`) into paragraphs and drop empty ones.
# 2. A paragraph that starts with `#` is a heading. Glue it to the paragraph after it with a newline, so a heading is never
#    the last line of a piece.
# 3. Add paragraphs to the current piece, separated by a blank line. When the next one would make the piece longer than
#    `size`, finish the piece and start a new one.
# 4. A new piece starts with the last `overlap` characters of the previous piece (cut at a space, so no half words), but only
#    when that still leaves room: the piece must never exceed `size`.
# 5. A single paragraph longer than `size` falls back to `chunk_fixed`.

# %%
def chunk_paragraphs(text, size=400, overlap=50):
    """Return a list of strings, each at most `size` characters, built from whole paragraphs where possible."""
    #### SOLUTION chunk_paragraphs
    paras, heading = [], ""
    for para in (p.strip() for p in text.split("\n\n")):
        if not para:
            continue
        if para.startswith("#"):
            heading = para + "\n"
            continue
        paras.append(heading + para)
        heading = ""
    if heading:
        paras.append(heading.strip())

    chunks, current = [], ""
    for para in paras:
        if len(para) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(chunk_fixed(para, size, overlap))
        elif current and len(current) + 2 + len(para) > size:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            tail = tail[tail.find(" ") + 1:] if " " in tail else ""
            current = tail + "\n\n" + para if tail and len(tail) + 2 + len(para) <= size else para
        else:
            current = current + "\n\n" + para if current else para
    if current:
        chunks.append(current)
    return chunks
    #### STUDENT
    # TODO 1: build `paras`, a list of paragraphs with each heading glued to the paragraph after it
    # TODO 2: pack them into `current` until the next one would not fit, then append `current` to `chunks`
    # TODO 3: start the next piece with an overlap tail, if it fits
    # TODO 4: a paragraph longer than size goes through chunk_fixed
    return []
    #### END

# %% [markdown]
# **Check 2.**

# %%
for name, text in docs.items():
    got = chunk_paragraphs(text, 500, 50)
    check(f"{name}: pieces fit in 500 characters", got and all(len(p) <= 500 for p in got), "a piece grew past size: check the overlap rule (step 4)")
sec = chunk_paragraphs(docs["security_policy"], 500, 50)
check("a heading is never the last line of a piece", all(not p.rstrip().splitlines()[-1].startswith("#") for p in sec), "glue a heading to the paragraph after it")
paras = [p.strip() for p in docs["security_policy"].split("\n\n") if p.strip() and not p.startswith("#")]
check("every paragraph of the security policy appears whole in some piece", all(any(p in c for c in sec) for p in paras),
      "do not cut inside a paragraph unless it is longer than size")
check("a long paragraph falls back to fixed pieces", len(chunk_paragraphs("word " * 300, 400, 50)) > 1, "paragraphs longer than size go through chunk_fixed")

# %% [markdown]
# ### Compare the two
#
# More pieces means more vectors to store and search, and shorter context in each.

# %%
print(f"{'size':>6} {'fixed':>7} {'paragraphs':>11}")
for size in (200, 400, 800):
    n_fixed = sum(len(chunk_fixed(t, size, 50)) for t in docs.values())
    n_para = sum(len(chunk_paragraphs(t, size, 50)) for t in docs.values())
    print(f"{size:>6} {n_fixed:>7} {n_para:>11}")
print()
print(chunk_paragraphs(docs["vendor_risk_management"], 500, 50)[2])

# %% [markdown]
# ## Exercise 3: a decision, not a function
#
# You will pick a chunk size again in notebook 7, with numbers. For now, decide with reasons. Write two or three sentences
# for each situation. There is no single right answer, and "it depends" is only acceptable with the dependency named.
#
# 1. A 400-page legal contract where each clause refers to defined terms elsewhere.
# 2. A FAQ where every entry is two lines.
# 3. A support chat log where meaning depends on the previous five messages.
#
# *Your answers:*

# %% [markdown]
# ## What to take away
#
# - A chunker is about 20 lines. There is no magic in it, and the choices you made (size, overlap, where to cut) change
#   what the search can find.
# - Cutting on structure (paragraphs, headings) beats cutting on character count for documents that have structure.
# - Overlap is insurance against a cut in the wrong place. It costs storage and repeated text in the prompt.
# - Real documents are harder: tables, PDFs with columns, code. The same two ideas apply.
#
# **Next:** notebook 2 turns each piece into a list of numbers so that a computer can compare meanings.
