"""Measure the payload delta of 'evidence once' from the chapter 2 request traces."""
import json, os, sys, re, statistics
from collections import defaultdict
sys.path.insert(0, 'src')
from arctic_qa.util import canonical_json
D='/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts'
names=json.load(open(sys.argv[1]))
DUP_LINE='alternatives, evidence, and the question claim type. '
per_stage=defaultdict(lambda: {'calls':0,'prompt_chars':0,'new_prompt_chars':0,'prompt_tokens':0,'system_chars':0,'removed_chars':0,'usd':0.0,'input_usd':0.0,'max_prompt_chars':0,'max_prompt_tokens':0,'no_trace':0,'parsed':0,'ratios':[]})
sizes=[]
def strip_source_data(prompt, stage):
    """Return (new_prompt, removed_chars) after dropping the duplicated text field."""
    m=re.search(r'SOURCE_DATA_BEGIN\n(.*?)\nSOURCE_DATA_END', prompt, re.S)
    if not m:
        return prompt, 0
    payload=json.loads(m.group(1))
    if 'chunks' in payload:
        for chunk in payload['chunks']:
            chunk.pop('text', None)
    else:
        payload.pop('text', None)
    new=prompt[:m.start(1)]+canonical_json(payload)+prompt[m.end(1):]
    return new, len(prompt)-len(new)
for n in names:
    r=json.load(open(os.path.join(D,n)))
    st=r['stage']; s=per_stage[st]; s['calls']+=1
    u=r.get('usage') or {}
    pt=u.get('promptTokenCount') or 0
    s['prompt_tokens']+=pt; s['usd']+=float(r.get('actual_cost_usd') or 0)
    tp=os.path.join(D, n[:-5]+'.request-trace.json')
    if not os.path.exists(tp):
        s['no_trace']+=1; continue
    t=json.load(open(tp))['payload']
    prompt=t['contents'][0]['parts'][0]['text']; system=t['systemInstruction']['parts'][0]['text']
    new, removed = strip_source_data(prompt, st)
    if st=='answer_verification':
        # the duplicated verifier line appears twice today; remove one copy
        if new.count(DUP_LINE)>=2:
            new=new.replace(DUP_LINE, '', 1); removed+=len(DUP_LINE)
    s['parsed']+=1; s['prompt_chars']+=len(prompt); s['new_prompt_chars']+=len(new); s['system_chars']+=len(system); s['removed_chars']+=removed
    s['max_prompt_chars']=max(s['max_prompt_chars'], len(prompt)); s['max_prompt_tokens']=max(s['max_prompt_tokens'], pt)
    if pt: s['ratios'].append(len(prompt)/pt)
    if st=='finding_answer_extraction': sizes.append((len(prompt), pt, len(new)))
PRICE_IN={'gemini-3.8-flash':0.75,'gemini-3.1-pro-preview':2.00,'gemini-3.1-flash-lite':0.25}
STAGE_MODEL={'finding_answer_extraction':'gemini-3.8-flash','question_generation':'gemini-3.8-flash','distractor_generation':'gemini-3.8-flash','repair':'gemini-3.8-flash','eligibility':'gemini-3.8-flash','standalone_verification':'gemini-3.1-pro-preview','blinded_reconstruction':'gemini-3.1-pro-preview','answer_verification':'gemini-3.1-pro-preview','option_verification':'gemini-3.1-pro-preview','answer_agreement':'gemini-3.1-flash-lite'}
out={}
total_saving=0.0
for st,s in sorted(per_stage.items()):
    ratio=statistics.median(s['ratios']) if s['ratios'] else None
    tokens_removed = s['removed_chars']/ratio if ratio else 0
    saving = tokens_removed/1e6*PRICE_IN[STAGE_MODEL[st]]
    total_saving+=saving
    out[st]={'calls':s['calls'],'traces_parsed':s['parsed'],'prompt_tokens':s['prompt_tokens'],'prompt_chars':s['prompt_chars'],'new_prompt_chars':s['new_prompt_chars'],'removed_chars':s['removed_chars'],'removed_share':round(s['removed_chars']/s['prompt_chars'],4) if s['prompt_chars'] else None,'median_chars_per_token':round(ratio,3) if ratio else None,'estimated_tokens_removed':round(tokens_removed),'input_price_usd_per_m':PRICE_IN[STAGE_MODEL[st]],'estimated_input_saving_usd':round(saving,4),'stage_usd':round(s['usd'],4),'max_prompt_chars':s['max_prompt_chars'],'max_prompt_tokens':s['max_prompt_tokens'],'system_chars_total':s['system_chars']}
out['_total_estimated_input_saving_usd']=round(total_saving,4)
sizes.sort()
out['_extractor_prompt_chars_percentiles']={p: sizes[int(len(sizes)*p/100)-1 if p==100 else int(len(sizes)*p/100)][0] for p in (50,90,95,99,100)}
out['_extractor_prompt_tokens_percentiles']={p: sorted(x[1] for x in sizes)[int(len(sizes)*p/100)-1 if p==100 else int(len(sizes)*p/100)] for p in (50,90,95,99,100)}
out['_extractor_new_prompt_chars_max']=max(x[2] for x in sizes)
print(json.dumps(out, indent=1))
json.dump(out, open(sys.argv[2],'w'), indent=1)
