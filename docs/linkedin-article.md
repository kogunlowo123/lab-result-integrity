A potassium of 6.8 is either a medical emergency or a hemolysed tube. The number alone cannot tell you which.

Labs already hold the data that can: the patient's last result, the serum indices, the calcium in the same tube, the drift of results on one instrument. But those checks live in different systems, and the evidence behind a hold is rarely in one place.

Why it matters: integrity errors reach clinicians as confident numbers. And no lab should let an agent release a result on its own judgement.

The flow we built:
Results → Trend analysis → Interference detection → Patient-based QC → Evidence collection → Pathologist review → Human decision

How it works:
1. Delta checks compare each result with the patient's previous one, by absolute or percent change inside a window set per analyte.
2. Reference and critical ranges depend on age band and sex. Unknown sex uses the narrowest combined interval.
3. HIL indices and cross-analyte rules catch hemolysis, EDTA carry-over and samples drawn above a drip.
4. A moving average per instrument flags drift, then lists the results already affected.
5. Every hold produces a hashed evidence packet that cites the source behind each rule. A pathologist claims the case and decides.
6. Agents reach it over MCP and A2A. They can screen and propose. They cannot claim, decide or release.

Best practices: thresholds are governed configuration with citations, and production refuses an unapproved catalogue. The service will not start under a policy that lets an agent release. Every decision goes into a signed audit chain. An optional model narrative only ever sees de-identified facts.

Tradeoffs: more cases reach humans than an autoverifying agent would send, a busy instrument causes database retries, and each lab must author and verify its own catalogue.

Value: pathologists open a case with the evidence already gathered, and auditors can see exactly which rule and which policy produced a hold.

As agents spread through diagnostics, the useful line is not "can the model judge this result" but "who signs, and on what evidence".

Question for lab leaders: should patient-based QC alarms ever stop an analyzer automatically, or always wait for a human?

Code: github.com/kogunlowo123/lab-result-integrity

#AI #ArtificialIntelligence #GenerativeAI #AgenticAI #LLMOps #EnterpriseAI #AISecurity #RAG #MachineLearning #DataScience #CloudComputing #Kubernetes #MLOps #DigitalTransformation #Innovation #LabMedicine #Pathology #HealthTech
