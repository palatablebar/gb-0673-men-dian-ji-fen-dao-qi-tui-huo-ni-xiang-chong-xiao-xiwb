def audit_refunds(ledger):
    spends={r['id']:r for r in ledger['spends']};issues=[];rows=[];cumulative={}
    for refund in ledger['refunds']:
        original=spends[refund['ref']]
        allocated=sum(x['points'] for x in refund['allocations'])
        valid=(allocated==refund['points']
               and refund['restored']+refund['expired_refund']==refund['points']
               and original['refunded']<=original['points'])
        cumulative[refund['ref']]=cumulative.get(refund['ref'],0)+refund['points']
        rows.append({k:v for k,v in refund.items() if k!='allocations'}
                    | {'spend_points':original['points'],
                       'cumulative_refunded':cumulative[refund['ref']],
                       'code':'VERIFIED' if valid else 'REFUND_UNBALANCED'})
        if not valid:issues.append(refund['id'])
    return rows,issues

def build_trails(ledger):
    # 批次级回拨轨迹：每笔退货在每个原批次上的去向（恢复可用或过期损失），可逐行追溯。
    refund_trail=[];restored_by_lot={};expired_by_lot={}
    expiry={lot['id']:lot['expires'] for lot in ledger['lots']}
    for refund in ledger['refunds']:
        for allocation in refund['allocations']:
            lot=allocation['lot']
            if allocation['expired']:
                expired_by_lot[lot]=expired_by_lot.get(lot,0)+allocation['points']
                status='已过期不恢复'
            else:
                restored_by_lot[lot]=restored_by_lot.get(lot,0)+allocation['points']
                status='已恢复可用'
            refund_trail.append({'refund_id':refund['id'],'member':refund['member'],'ref':refund['ref'],
                'lot':lot,'points':allocation['points'],'status':status,
                'expires':expiry.get(lot,allocation.get('expires','')),'at':refund['at'],'line':refund['line']})
    # 批次余额轨迹：发放、累计消费、退货恢复、过期退款、自然到期与剩余可用逐批对账。
    natural_expired={}
    for event in ledger['ledger']:
        if event['action']=='EXPIRE':
            natural_expired[event['id']]=natural_expired.get(event['id'],0)+event['points']
    consumed_by_lot={}
    for spend in ledger['spends']:
        for allocation in spend['allocations']:
            consumed_by_lot[allocation['lot']]=consumed_by_lot.get(allocation['lot'],0)+allocation['points']
    lot_trail=[]
    for lot in ledger['lots']:
        lid=lot['id']
        row={'lot':lid,'member':lot['member'],'issued':lot['issued'],
             'consumed':consumed_by_lot.get(lid,0),
             'restored_refund':restored_by_lot.get(lid,0),
             'expired_refund':expired_by_lot.get(lid,0),
             'natural_expired':natural_expired.get(lid,0),
             'remaining':lot['remaining'],'expires':lot['expires']}
        # 发放 = 剩余可用 + 累计消费 − 退货恢复 + 自然到期；过期退款本就未复活，不计入可用。
        row['balanced']=(row['issued']==row['remaining']+row['consumed']-row['restored_refund']+row['natural_expired'])
        lot_trail.append(row)
    return refund_trail,lot_trail

def apply(state):
    ledger=state['point_ledger']
    rows,issues=audit_refunds(ledger)
    refund_trail,lot_trail=build_trails(ledger)
    state['refund_audit']=rows;state['refund_issues']=issues
    state['refund_trail']=refund_trail;state['lot_trail']=lot_trail
    state['views']['refund']={'title':'退货回拨','rows':rows,'trail':refund_trail,'lots':lot_trail,
        'metrics':{'退款笔':len(rows),
                   '恢复积分':sum(x['restored'] for x in rows),
                   '过期不恢复':sum(x['expired_refund'] for x in rows),
                   '回拨明细行':len(refund_trail),
                   '批次轨迹':len(lot_trail)}}
