# Seeded document extractions

What a document extractor produced from a signed shipping document, stored as
the extractor's output rather than as a PDF.

**Why fixtures rather than real PDFs in Phase 1.** The conflict these files
carry — the ERP says one temperature envelope, the signed Bill of Lading says
another — is a *reconciliation* property, and it has to be testable long
before there is a vision model. Generating PDFs now and parsing them would
couple the evidence pipeline's first tests to an extraction stack that does
not exist yet, and would make the scenario's outcome depend on OCR quality
rather than on the conflict rule under test.

Phase 4 replaces the `extractor` field with a real multimodal extraction and
keeps this file shape. That is the point of the shape: the adapter downstream
does not change.

**`extraction_confidence` is not a confidence.** It is one input to the
taxonomy's confidence computation, alongside source reliability and age.
Nothing here supplies a final confidence, and nothing may — see invariant I2.

**Page and bounding box are load-bearing.** They are what makes a number in a
recommendation click through to the place on the page it came from. An
extraction without them is an assertion; with them it is a citation.
