import json,re,sys,glob
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
sys.path.insert(0,'grafana')
import validate_dashboards as v
schema={
 'flight':"session_id gap_segment span_part cid callsign logon_time name home_icao server departure arrival aircraft_icao aircraft_faa flight_rules route remarks planned_alt deptime pilot_rating military_rating start_time end_time duration duration_minutes airborne_minutes max_gs_kt max_alt_ft sample_count is_circuit is_zzzz touches_home_region",
 'controller_session_merged':"cid callsign merge_group first_session_id session_count name rating facility server frequency_hz start_time end_time duration duration_hours is_open",
 'online_now':"kind session_id cid callsign name logon_time first_seen last_seen as_of departure arrival aircraft_icao lat lon alt_ft gs_kt rating facility frequency_hz",
 'data_freshness':"checked_at latest_snapshot_ts snapshot_age_seconds data_since last_ok_poll_at last_ok_age_seconds ok_last_hour skipped_last_hour error_last_hour",
 'controller_rating':"id short long",'facility':"id short long",'pilot_rating':"id short long",'airline':"prefix name",'aircraft_type':"icao category is_military",'region_map':"prefix region",
 'app_setting':"key value",
 'network_snapshot':"observed_at pilots_in_region controllers_in_region pilots_global controllers_global",
 'poll_log':"id polled_at status snapshot_ts duration_ms http_status pilots_seen pilots_inside pilots_with_plan pilots_kept controllers_seen controllers_kept rejected_values polygon_sha whitelist_sha error",
 'pilot_session':"id cid callsign logon_time name home_icao server first_seen last_seen departure arrival aircraft_icao aircraft_faa flight_rules route remarks planned_alt deptime plan_changed_at max_gs_kt max_alt_ft pilot_rating military_rating",
 'pilot_position':"observed_at session_id lat lon alt_ft gs_kt hdg",
 'controller_session':"id cid callsign logon_time name rating facility server frequency_hz visual_range first_seen last_seen",
 'controller_unlisted_sighting':"callsign cid first_seen last_seen polls_seen",
}
schema={k:{c:'text' for c in s.split()} for k,s in schema.items()}
# derive the view columns from db/migrations/*.sql in order (later views depend on earlier ones;
# a view redefined by a later migration replaces the earlier definition)
def migration_statements():
    for f in sorted(glob.glob("db/migrations/*.sql")):
        body = re.sub(r"(?is)\$(\w*)\$.*?\$\1\$", "'body'", open(f, encoding="utf-8").read())  # dollar-quoted bodies
        try:
            yield from sqlglot.parse(body, read='postgres')
        except Exception as e:  # sqlglot may not know every PostgreSQL construct
            print('PARSE ERR', f, str(e).splitlines()[0][:160])
funcs=set()
for st in migration_statements():
    if st is None: continue
    if isinstance(st,exp.Create) and st.args.get('kind')=='VIEW':
        name=st.this.name
        q=st.expression
        try:
            qq=qualify(q.copy(),schema={k:dict(c) for k,c in schema.items()},dialect='postgres',validate_qualify_columns=True)
            schema[name]={c:'text' for c in qq.named_selects}
        except Exception as e:
            print('VIEW ERR',name,str(e)[:200])
print({k:len(x) for k,x in schema.items()})
bad=0
for f in sorted(glob.glob('grafana/dashboards/*/*.json')):
    d=json.load(open(f,encoding='utf-8'))
    for label,sql,_ in v.sql_items(d):
        s=v.substitute_macros(sql)
        try:
            t=sqlglot.parse_one(s,read='postgres')
            qualify(t,schema={k:dict(c) for k,c in schema.items()},dialect='postgres',validate_qualify_columns=True)
        except Exception as e:
            bad+=1; print(f,label,str(e)[:200])
print('bad',bad)
