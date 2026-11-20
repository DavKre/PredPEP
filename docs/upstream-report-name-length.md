# For the dev: the submission form caps the run name at 4 characters, the API does not

Found while merging v1.6.0 onto the fleet node. It is in your code as well as
ours, which is why it is written up here rather than only fixed locally.

## The inconsistency

`app/templates/index.html` caps the protein symbol in the browser:

```html
<label for="protein_symbol">Protein symbol</label>
<input type="text" id="protein_symbol" name="protein_symbol"
       placeholder="e.g. TYK2" maxlength="4" required>
```

`POST /upload` applies no such limit — `request.form.get('protein_symbol')` is
stored verbatim in `job.json` and rendered untruncated by the jobs table. So the
same field has two different rules depending on how a job arrives, and each group
of users only ever sees its own.

Identical in v1.4.0 and v1.6.0, and `user_name` next to it has no `maxlength` at
all — which is why `beeha.gan` works while `TYK2A` does not.

## Why it matters in practice

On the production node (6363), **49 of 72 jobs carry a name longer than four
characters** — every one submitted by script:

```
ESM_pep02_fbn_2nd_try       21 chars   Hippo
best_cluster_design_5_DR3   25 chars   Hippo
Nectin4-P3                  10 chars   Thissa
NT5_10_29                    9 chars   NT5_10_29
P8New2                       6 chars   Thissa
```

So the long-name workflow is the normal one here, and the browser is the only
path that cannot do it. A user testing the web form reported "I can only type
four characters" and was correct — while the jobs table in front of him showed
twenty-five-character names submitted by someone else.

## Why the cap looks unnecessary

`protein_symbol` never reaches a path or a command line. `generate_base_name()`
takes the first three *letters* for the job id:

```python
letters = re.sub(r'[^a-zA-Z]', '', protein_symbol)
prot_part = letters[:3].upper() if letters else 'XXX'
```

and the raw string only goes into `job.json` metadata and the jobs table. Raising
the form limit changes nothing server-side.

Worth noting separately: because the id keeps only letters, `P53` becomes `SPP<u>`
and `ESM_pep02_fbn` and `ESM_pep02_fbn_2nd_try` both become `SPESM<u>`. Job ids
stay unique through the uuid suffix, so this is a readability matter in folder
names, not a correctness one — but it is why the typed name is the only thing
that actually distinguishes two runs on the same target.

## The change we made

Four lines in `app/templates/index.html`, nothing else:

```diff
-                <label for="protein_symbol">Protein symbol</label>
-                <input type="text" id="protein_symbol" name="protein_symbol" placeholder="e.g. TYK2" maxlength="4" required>
+                <label for="protein_symbol">Protein symbol or run name</label>
+                <input type="text" id="protein_symbol" name="protein_symbol" placeholder="e.g. TYK2, or ESM_pep02_fbn_2nd_try" maxlength="64" required>
```

64 is generous against the longest name in use (25) and still bounded, since the
value is stored per job and rendered in a table. The job id is deliberately
untouched: the base name is embedded in model filenames, `.spec` files and
`pdb_relative_path` entries that `run_createflags.py` and the aggregation parse,
so widening *that* would be a real change; widening what a user may type is not.

Verified on a built image: the form serves `maxlength="64"`, a 25-character name
posted the way the form posts it is accepted, stored whole in `job.json`, and
returned whole by `/jobs`; the id came back `SPBESB_f0e71f0b` as expected.

## The ask

Take this upstream if you agree with it. It is currently our only divergence from
your front end — `index.html` is otherwise byte-identical, which is what lets
your UI releases drop into the fleet node without a merge, and we would rather
keep it that way than carry a patch that your next release silently reverts.
