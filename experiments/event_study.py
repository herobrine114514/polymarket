"""Finite macro/daily engineering pilot, with shared V4 Pro budget and no orders.

Prepare freezes observed evidence and separate public market baselines. Run
compares the existing control to a structured blind prompt on the same packet.
No model selection, formal prospective registration or unattended repeat runs.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from polycast.config import Config
from polycast.event_research import (INPUT_VERSION,STRUCTURED_PROMPT_VERSION,checked_forecast,control_payload,
    encoded,forecast_schema,freeze_packet,instant,make_packet,model_view,observations_at,publish_once,
    read_packet,sha,structured_prompt,verify_packet_observations,SETTLEMENT_CONTROL_VERSION,
    settlement_control_prompt,settlement_control_schema,checked_settlement_control,audit_daily_schedule)
from polycast.store import read_database


def collect_observations(config:Config) -> dict:
    """Fetch configured/official summaries before acquiring a database writer."""
    from polycast.evidence import collect_official,collect_configured
    from polycast.social import collect_fed_bluesky
    from polycast.short_cycle import _mlb_snapshot,MLB
    from polycast.store import database,insert_evidence,sync_news_sources,_upsert_source
    with read_database(config.database) as conn:
        daily=[dict(r) for r in conn.execute("SELECT * FROM markets WHERE in_watchlist=1 AND category='daily'")]
    items,errors=collect_official();social,social_errors=collect_fed_bluesky()
    extra,extra_errors,sources=collect_configured(config.data_dir/'sources.json')
    items.extend(social);items.extend(extra);errors.extend(social_errors);errors.extend(extra_errors)
    for market in daily:
        try:
            snapshot=_mlb_snapshot(market,datetime.now(timezone.utc))
            if snapshot:items.append(snapshot)
        except (ValueError,KeyError,RuntimeError,OSError) as exc:errors.append('MLB: '+type(exc).__name__)
    with database(config.database) as conn:
        sync_news_sources(conn,sources)
        _upsert_source(conn,'mlb_schedule','MLB Stats API','official',MLB,'official_source',MLB,'',1)
        before=conn.execute('SELECT count(*) FROM evidence_observations').fetchone()[0]
        inserted=insert_evidence(conn,items)
        after=conn.execute('SELECT count(*) FROM evidence_observations').fetchone()[0]
    return {'status':'finite_event_source_collection','fetched':len(items),'new_versions':inserted,
        'new_observations':after-before,'total_observations':after,'errors':errors,'model_calls':0}


def prepare(config: Config,event_id: str,output: Path,*,capture_baselines=True) -> dict:
    from polycast.market import book_baseline,order_book
    with read_database(config.database) as conn:
        markets=[dict(r) for r in conn.execute("SELECT * FROM markets WHERE event_id=? AND in_watchlist=1 ORDER BY id",(event_id,))]
    if not markets or markets[0]['category'] not in ('macro','daily'):
        raise ValueError('No macro/daily event in the watchlist')
    baselines={}
    if capture_baselines:
        for m in markets:
            try:
                raw=order_book(m['yes_token']);snapshot=book_baseline(raw,10)
                received=datetime.now(timezone.utc).isoformat()
                if snapshot is None:
                    baselines[m['id']]={'status':'unavailable','reason':'no_fresh_paired_book','received_at':received,'raw_book':raw}
                else:
                    source,bid,ask=snapshot
                    baselines[m['id']]={'status':'captured','received_at':received,'book_timestamp':source,
                        'bid':bid,'ask':ask,'p_yes_mid':(bid+ask)/2,'spread':ask-bid,'raw_book':raw}
            except (OSError,ValueError,RuntimeError) as exc: baselines[m['id']]={'status':'unavailable','reason':type(exc).__name__}
    cutoff=datetime.now(timezone.utc).isoformat()
    for baseline in baselines.values():
        if baseline.get('status')=='captured' and (not 0<=(instant(cutoff)-instant(baseline['received_at'])).total_seconds()<=10 or
                not 0<=(instant(cutoff)-instant(baseline['book_timestamp'])).total_seconds()<=10):
            baseline.update(status='unavailable',reason='baseline_outside_shared_cutoff_age')
    with read_database(config.database) as conn:
        conn.execute('BEGIN')
        # Contract updates during the public baseline reads are detected before export.
        current=[dict(r) for r in conn.execute("SELECT * FROM markets WHERE event_id=? AND in_watchlist=1 ORDER BY id",(event_id,))]
        if current!=markets:raise ValueError('Contract changed while preparing research input')
        obs,audit=observations_at(conn,cutoff)
        packet=make_packet(markets,obs,cutoff,audit)
    from experiments.mlb_evidence import verify_packet_captures
    packet['input_audit']['official_mlb_capture_proof'] = verify_packet_captures(packet, config.data_dir)
    from experiments.macro_evidence import verify_packet_captures as verify_macro_captures
    packet['input_audit']['official_fomc_capture_proof'] = verify_macro_captures(packet, config.data_dir)
    if packet['category'] == 'macro' and packet['input_audit']['official_fomc_capture_proof']['captures']:
        from polycast.macro_grounding import build_registry
        registry = build_registry(packet)
        packet['input_audit']['macro_fact_registry'] = registry
        packet['input_audit']['macro_fact_registry_sha256'] = sha(registry)
    wrapper=freeze_packet(output,packet,baselines)
    return {'status':'frozen_research_input','packet_sha256':wrapper['packet_sha256'],'event_id':event_id,
        'evidence_groups':len(packet['evidence_groups']),'input_audit':packet['input_audit'],
        'model_calls':0,'prospective_registered':False,'live_ready':False}


def collect_mlb_statistics(config: Config, event_id: str) -> dict:
    """One event, five public requests; all network work precedes the DB writer."""
    import uuid
    from experiments.mlb_pregame import collect
    from experiments.mlb_evidence import DIRECTORY, SOURCE_ID, FEED_URL, evidence_item
    from polycast.store import database, insert_evidence, _upsert_source
    with read_database(config.database) as conn:
        markets = [dict(row) for row in conn.execute(
            "SELECT * FROM markets WHERE event_id=? AND in_watchlist=1 AND category='daily'", (event_id,))]
    if len(markets) != 1 or not markets[0].get('source_ref', '').isdigit():
        raise ValueError('One official MLB daily event is required')
    market = markets[0]
    if instant(market['end_at']) <= datetime.now(timezone.utc):
        raise ValueError('MLB event is already at its prediction cutoff')
    name = 'capture-' + uuid.uuid4().hex
    root = config.data_dir / DIRECTORY / name
    collect(root, int(market['source_ref']))
    item = evidence_item(config.data_dir, name, market)
    with database(config.database) as conn:
        current = [dict(row) for row in conn.execute(
            "SELECT * FROM markets WHERE event_id=? AND in_watchlist=1 AND category='daily'", (event_id,))]
        if current != markets:
            raise ValueError('Daily contract changed during official statistics collection')
        _upsert_source(conn, SOURCE_ID, item['source'], 'official', FEED_URL, 'official_source', FEED_URL, '', 1)
        before = conn.execute('SELECT count(*) FROM evidence_observations').fetchone()[0]
        inserted = insert_evidence(conn, [item], activate_current=False)
        observations = conn.execute('SELECT count(*) FROM evidence_observations').fetchone()[0] - before
    return {'protocol': 'mlb-pregame-evidence-v1', 'status': 'official_statistics_observed',
            'event_id': event_id, 'game_id': int(market['source_ref']), 'capture_directory': name,
            'new_evidence_versions': inserted, 'new_observations': observations,
            'source_capture': item['capture_descriptor'], 'model_calls': 0, 'orders_submitted': 0,
            'historical_backfill': False, 'prospective_registered': False, 'live_ready': False}


def collect_macro_history(config: Config, event_id: str) -> dict:
    """Finite official FOMC capture; history is observed now, not backfilled."""
    import uuid
    from experiments.macro_policy import collect
    from experiments.macro_evidence import DIRECTORY
    from polycast.market import complete_fed_event
    with read_database(config.database) as conn:
        markets = [dict(row) for row in conn.execute(
            "SELECT * FROM markets WHERE event_id=? AND in_watchlist=1 AND category='macro' ORDER BY id", (event_id,))]
    if not complete_fed_event(markets) or instant(markets[0]['end_at']) <= datetime.now(timezone.utc):
        raise ValueError('A future complete five-state Fed event is required')
    name = 'capture-' + uuid.uuid4().hex
    collect(config.data_dir / DIRECTORY / name)
    return observe_macro_capture(config, event_id, name, expected_markets=markets)


def observe_macro_capture(config: Config, event_id: str, name: str, *, expected_markets=None) -> dict:
    """Import an already-complete verified capture, without repeating requests."""
    from experiments.macro_evidence import SOURCE_ID, CALENDAR, evidence_item, read_json, capture_path
    from experiments.macro_policy import contract_context
    from polycast.store import database, insert_evidence, _upsert_source
    from polycast.market import complete_fed_event, fed_outcome_slot
    with read_database(config.database) as conn:
        markets = [dict(row) for row in conn.execute(
            "SELECT * FROM markets WHERE event_id=? AND in_watchlist=1 AND category='macro' ORDER BY id", (event_id,))]
    if not complete_fed_event(markets) or expected_markets is not None and expected_markets != markets:
        raise ValueError('Macro contract changed or lacks five complete states')
    item = evidence_item(config.data_dir, name)
    result, _ = read_json(capture_path(config.data_dir, name)/'result.json')
    context = contract_context({'category': 'macro', 'kind': 'exclusive_multiple_choice',
        'as_of': datetime.now(timezone.utc).isoformat(), 'end_at': markets[0]['end_at'],
        'resolution_rules': markets[0]['description'],
        'options': [{'key': m['id'], 'label': fed_outcome_slot(m['question'])} for m in markets]}, result['facts'])
    with database(config.database) as conn:
        current = [dict(row) for row in conn.execute(
            "SELECT * FROM markets WHERE event_id=? AND in_watchlist=1 AND category='macro' ORDER BY id", (event_id,))]
        if current != markets: raise ValueError('Macro contract changed during official policy capture')
        _upsert_source(conn, SOURCE_ID, item['source'], 'official', CALENDAR, 'official_source', CALENDAR, '', 1)
        before = conn.execute('SELECT count(*) FROM evidence_observations').fetchone()[0]
        inserted = insert_evidence(conn, [item], activate_current=False)
        observations = conn.execute('SELECT count(*) FROM evidence_observations').fetchone()[0] - before
    return {'protocol': 'fomc-history-evidence-v1', 'status': 'official_policy_history_observed',
            'event_id': event_id, 'capture_directory': name, 'statements': len(result['facts']['statements']),
            'new_evidence_versions': inserted, 'new_observations': observations, 'source_capture': item['capture_descriptor'],
            'research_baseline': result['research_baseline'], 'contract_context': context,
            'model_calls': 0, 'orders_submitted': 0,
            'historical_backfill': False, 'prospective_registered': False, 'live_ready': False}


def framework_mapping(packet:dict) -> dict:
    from importlib.metadata import version
    if version('forecasting-tools')!='0.3.1':raise ValueError('Use the pinned isolated forecasting-tools environment')
    from forecasting_tools import BinaryQuestion,MultipleChoiceQuestion
    target=packet.get('forecast_target') or {}
    view=model_view(packet);background=view['evidence_groups']
    if 'macro_fact_registry' in view:
        background={k:view[k] for k in ('evidence_groups','macro_fact_registry','macro_fact_registry_sha256')}
    shared={'resolution_criteria':packet['resolution_rules'],'scheduled_resolution_time':instant(packet['end_at']),
        'background_info':encoded(background),
        'fine_print':f"Evidence cutoff {packet['as_of']}. Research only; no market prices or publishing. Target: {encoded(target)}"}
    if packet['kind']=='binary':
        condition=" Conditional on the event resolving with a named winner." if target.get('fractional_settlement_possible') else ""
        question=BinaryQuestion(question_text=f"Will {packet['options'][0]['label']} win {packet['event_title']} under the supplied rules?"+condition,**shared)
    else:
        question=MultipleChoiceQuestion(question_text=packet['event_title'],options=[o['label'] for o in packet['options']],**shared)
    return {'status':'framework_question_mapping_ok','framework':'forecasting-tools','version':'0.3.1',
        'question_type':type(question).__name__,'question_text':question.question_text,
        'resolution_criteria':question.resolution_criteria,'background_sha256':hashlib.sha256(question.background_info.encode()).hexdigest(),
        'forecast_target':target,
        'macro_fact_registry_sha256':view.get('macro_fact_registry_sha256'),
        'model_calls':0,'default_framework_research_or_publication_invoked':False}


def run(config:Config,packet_path:Path,output:Path,*,max_calls=2,require_daily_statistics=True,require_macro_history=True) -> dict:
    from polycast.analysis import (DEEPSEEK_MODEL,PROMPT_VERSION,ProviderUnavailable,_run_json,run_codex,run_event_codex)
    from polycast.billing import capture_usage,save_model_usage
    from polycast.store import database,reserve_model_call,finish_model_call
    from polycast.forecast_audit import (VERSION as AUDIT_VERSION, state_proof, state_audit,
                                        macro_proof as macro_relation_proof, macro_audit, probability)
    from polycast.model_output import capture_model_output
    if max_calls!=2 or DEEPSEEK_MODEL!='deepseek-v4-pro':raise ValueError('Pilot requires exactly two V4 Pro calls within the shared daily budget')
    wrapper=read_packet(packet_path);packet=wrapper['packet']
    if packet['input_version']!=INPUT_VERSION:
        raise ValueError('Legacy packets remain readable for audit but cannot receive new pilot calls')
    time_audit=audit_daily_schedule(packet)
    if time_audit['status'] in ('conflict','ambiguous_rule_schedule','invalid_rule_schedule'):
        raise ValueError('Contract rule schedule conflicts with or cannot uniquely identify the frozen official game time')
    with read_database(config.database) as conn:proof=verify_packet_observations(conn,packet)
    from experiments.mlb_evidence import verify_packet_captures
    statistics_proof = verify_packet_captures(packet, config.data_dir)
    from experiments.macro_evidence import verify_packet_captures as verify_macro_captures
    macro_proof = verify_macro_captures(packet, config.data_dir)
    labelled_macro = packet['category'] == 'macro' and require_macro_history is True
    from polycast.macro_grounding import (PROMPT_VERSION as MACRO_GROUNDED_PROMPT_VERSION, checked_registry,
                                         schema as grounded_schema, prompt as grounded_prompt,
                                         audit_facts, checked_forecast as checked_grounded_forecast)
    if type(require_daily_statistics) is not bool:
        raise ValueError('Invalid daily statistics requirement')
    if packet['category'] == 'daily' and require_daily_statistics and not statistics_proof['captures']:
        raise ValueError('Full official pregame statistics are required for new daily pilot calls')
    if type(require_macro_history) is not bool: raise ValueError('Invalid macro history requirement')
    if packet['category'] == 'macro' and require_macro_history and not macro_proof['captures']:
        raise ValueError('Receipt-verified official policy history is required for new macro pilot calls')
    grounded_registry = checked_registry(packet) if labelled_macro else None
    now=datetime.now(timezone.utc)
    if not 0<=(now-instant(packet['as_of'])).total_seconds()<=300 or now>=instant(packet['end_at']):
        raise ValueError('Pilot input is stale or event cutoff has passed')
    source_names=('polycast/event_research.py','polycast/event_scoring.py','polycast/store.py','polycast/analysis.py','experiments/event_study.py',
                  'experiments/mlb_pregame.py','experiments/mlb_evidence.py',
                  'experiments/macro_policy.py','experiments/macro_evidence.py','polycast/evidence.py',
                  'polycast/forecast_audit.py','polycast/model_output.py','polycast/market.py','polycast/macro_grounding.py')
    root=Path(__file__).resolve().parents[1]
    source_bytes={n:(root/n).read_bytes() for n in source_names}
    source_bundle={'schema_version':1,'protocol':'event-study-source-bundle-v1',
        'files':{n:{'sha256':hashlib.sha256(blob).hexdigest(),'content_base64':base64.b64encode(blob).decode('ascii')}
                 for n,blob in source_bytes.items()}}
    source_bundle['bundle_sha256']=sha(source_bundle)
    start={'schema_version':1,'protocol':'macro-daily-engineering-pilot-v1','event_id':packet['event_id'],
        'packet_sha256':wrapper['packet_sha256'],'packet_file_sha256':hashlib.sha256(packet_path.read_bytes()).hexdigest(),
        'started_at':now.isoformat(),'max_model_calls':2,'model_version':DEEPSEEK_MODEL,
        'control_prompt_version':SETTLEMENT_CONTROL_VERSION if packet['kind']=='settlement_multiple_choice' else PROMPT_VERSION,
        'control_target_adapted':packet['kind']=='settlement_multiple_choice',
        'structured_prompt_version':MACRO_GROUNDED_PROMPT_VERSION if labelled_macro else STRUCTURED_PROMPT_VERSION,
        'input_version':INPUT_VERSION,'forecast_target':packet['forecast_target'],
        'rule_time_audit':time_audit,
        'official_mlb_capture_proof': statistics_proof, 'require_daily_statistics': require_daily_statistics,
        'official_fomc_capture_proof': macro_proof, 'require_macro_history': require_macro_history,
        'output_audit_protocol': AUDIT_VERSION,
        'output_relation_proofs': {'control': state_proof(packet),
            'structured': macro_relation_proof(packet) if labelled_macro else state_proof(packet)},
        'shared_macro_fact_registry_sha256': sha(grounded_registry) if grounded_registry else None,
        'typed_fact_assertions_audited_styles': ['structured'] if labelled_macro else [],
        'source_hashes':{n:item['sha256'] for n,item in source_bundle['files'].items()},
        'source_bundle_sha256':source_bundle['bundle_sha256'],
        'input_proof':proof,'prospective_registered':False,'orders_enabled':False}
    # Existing start receipt blocks repeats, including after a failed/partial run.
    publish_once(output/'started.json',start)
    publish_once(output/'source-bundle.json',source_bundle)
    receipts=[]
    for style in ('control','structured'):
        t=datetime.now(timezone.utc)
        if t>=instant(packet['end_at']) or (t-instant(packet['as_of'])).total_seconds()>300:
            receipt={'style':style,'status':'not_called_stale_or_ended','started_at':t.isoformat(),'model_call_id':None}
            publish_once(output/(style+'.json'),receipt);receipts.append(receipt);continue
        try:
            with database(config.database) as conn:
                call_id=reserve_model_call(conn,packet['market_ids'][0],'analysis',DEEPSEEK_MODEL,config.model_call_limit('deepseek'),'deepseek')
        except (RuntimeError,OSError) as exc:
            receipt={'style':style,'status':'not_called_budget_or_pause','started_at':t.isoformat(),
                'model_call_id':None,'error':type(exc).__name__+': '+str(exc)[:240]}
            publish_once(output/(style+'.json'),receipt);receipts.append(receipt);continue
        usage_items=[];raw_responses=[];output_audit=None;fact_audit=None;begin=time.monotonic();result=None;provider_result=None;error=None
        def usage_sink(item):
            with database(config.database) as conn:save_model_usage(conn,call_id,item)
            usage_items.append(item)
        def response_sink(item):
            # Capture before parsing/identity/probability checks. File publication
            # is exclusive; an invalid completion remains available for review.
            name = f'{style}-raw-response-{len(raw_responses)+1:02d}.json'
            record = {**item, 'style': style, 'model_call_id': call_id, 'packet_sha256': wrapper['packet_sha256']}
            publish_once(output/name, record)
            raw_responses.append({'filename': name, 'file_sha256': hashlib.sha256((output/name).read_bytes()).hexdigest(),
                **{k:item[k] for k in ('protocol','received_at','content_sha256','content_bytes','content_complete','strict_json_object_valid','error')}})
        try:
            with capture_usage(usage_sink), capture_model_output(response_sink):
                if style=='control':
                    if packet['kind']=='settlement_multiple_choice':
                        result=_run_json(settlement_control_prompt(packet),settlement_control_schema(packet),provider='deepseek',data_dir=config.data_dir,timeout_seconds=120)
                        checked_settlement_control(packet,result)
                    else:
                        payload=control_payload(packet)
                        result=(run_event_codex(payload,data_dir=config.data_dir,timeout_seconds=120) if packet['kind']!='binary'
                                else run_codex(payload,data_dir=config.data_dir,timeout_seconds=120))
                else:
                    if labelled_macro:
                        provider_result=_run_json(grounded_prompt(packet),grounded_schema(packet),provider='deepseek',data_dir=config.data_dir,timeout_seconds=120)
                        output_audit=macro_audit(packet,provider_result)
                        if not output_audit['accepted']:
                            raise ValueError('Macro probability relationship audit rejected the original output')
                        fact_audit=audit_facts(packet,provider_result)
                        result=checked_grounded_forecast(packet,provider_result)
                    else:
                        provider_result=_run_json(structured_prompt(packet),forecast_schema(packet),provider='deepseek',data_dir=config.data_dir,timeout_seconds=120)
                        result=provider_result
                        checked_forecast(packet,result)
                if output_audit is None:
                    if packet['kind'] == 'binary' and style == 'control':
                        p = result.get('raw_p_yes', result.get('p_yes'))
                        values = {'YES': p, 'NO': 1-p if probability(p) else None}
                    else:
                        values = result.get('raw_probabilities', result.get('probabilities'))
                    output_audit=state_audit(packet,values)
                if not output_audit['accepted']:
                    raise ValueError('Research probability relationship audit rejected the original output')
                if any(r['strict_json_object_valid'] is not True for r in raw_responses):
                    raise ValueError('Original completion is missing, truncated or invalid strict JSON')
        except Exception as exc:
            error=type(exc).__name__+': '+str(exc)[:240]
            pause=exc.retry_at.isoformat() if isinstance(exc,ProviderUnavailable) else None
            with database(config.database) as conn:finish_model_call(conn,call_id,error,pause,'deepseek')
        else:
            with database(config.database) as conn:finish_model_call(conn,call_id,provider='deepseek')
        finished=datetime.now(timezone.utc)
        valid=error is None and finished<instant(packet['end_at']) and (finished-instant(packet['as_of'])).total_seconds()<=300
        receipt={'schema_version':1,'style':style,'model_version':DEEPSEEK_MODEL,'model_call_id':call_id,
            'packet_sha256':wrapper['packet_sha256'],'as_of':packet['as_of'],'started_at':t.isoformat(),
            'published_at':finished.isoformat(),'elapsed_ms':round((time.monotonic()-begin)*1000),
            'status':'published_research_forecast' if valid else 'failed_or_late','error':error,
            'abstain':result.get('abstain') if result else None,'result':result,'usage_receipts':usage_items,
            'provider_result':provider_result,
            'output_probability_audit':output_audit,'raw_response_receipts':raw_responses,
            'output_fact_audit':fact_audit,
            'raw_response_capture_complete':len(raw_responses)==1 and raw_responses[0]['content_complete'] is True,
            'cost_status':'estimated_from_tokens' if usage_items and usage_items[0].get('cost_lower_usd') is not None else 'unknown_not_zero',
            'prospective_registered':False,'profitability_validated':False,'live_ready':False}
        publish_once(output/(style+'.json'),receipt);receipts.append(receipt)
    summary={'schema_version':1,'event_id':packet['event_id'],'packet_sha256':wrapper['packet_sha256'],
        'model_calls':sum(r.get('model_call_id') is not None for r in receipts),
        'paired_schema_valid_forecasts':all(r['status']=='published_research_forecast' and r.get('abstain') is False for r in receipts),
        'probability_audits_accepted':all((r.get('output_probability_audit') or {}).get('accepted') is True for r in receipts),
        'raw_response_capture_complete':all(r.get('raw_response_capture_complete') is True for r in receipts),
        'structured_typed_fact_audit_accepted':next(((r.get('output_fact_audit') or {}).get('accepted') for r in receipts if r['style']=='structured'),None),
        'semantic_review_completed':False,'paired_eligible_forecasts':False,
        'styles':[{k:r.get(k) for k in ('style','status','abstain','elapsed_ms','model_call_id','cost_status')} for r in receipts],
        'specialist_challenger_executed':False,'official_scores':None,'net_profit':None,'live_ready':False}
    publish_once(output/'summary.json',summary)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('collect');p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('collect-mlb');p.add_argument('--event-id',required=True);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('collect-macro');p.add_argument('--event-id',required=True);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('observe-macro-capture');p.add_argument('--event-id',required=True);p.add_argument('--capture-name',required=True);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('prepare');p.add_argument('--event-id',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--no-baselines',action='store_true')
    p=sub.add_parser('run');p.add_argument('--packet',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('framework-map');p.add_argument('--packet',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();config=Config()
    if args.command=='collect':result=collect_observations(config);publish_once(args.output,result)
    elif args.command=='collect-mlb':result=collect_mlb_statistics(config,args.event_id);publish_once(args.output,result)
    elif args.command=='collect-macro':result=collect_macro_history(config,args.event_id);publish_once(args.output,result)
    elif args.command=='observe-macro-capture':result=observe_macro_capture(config,args.event_id,args.capture_name);publish_once(args.output,result)
    elif args.command=='prepare':result=prepare(config,args.event_id,args.output,capture_baselines=not args.no_baselines)
    elif args.command=='run':result=run(config,args.packet,args.output)
    else:
        result=framework_mapping(read_packet(args.packet)['packet']);publish_once(args.output,result)
    print(json.dumps(result,ensure_ascii=False))
