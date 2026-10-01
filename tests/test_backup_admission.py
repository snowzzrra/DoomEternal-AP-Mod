import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from doom_eap.launcher.launcher_session import APSessionOwner


def test_completed_backup_still_requires_matching_native_owner_and_archive(tmp_path):
    owner=APSessionOwner(tmp_path,tmp_path/'data',tmp_path/'state')
    owner.namespace='a'*64
    owner._runtime=tmp_path
    scope={'ready':True,'pid':77,'process_created':99,'admission':{'instance_id':'c'*32}}
    owner.observe=Mock(return_value=scope)
    request={'operation':'native_backup_request','pid':77,'process_created':'99',
             'instance_id':'c'*32,'namespace':owner.namespace}
    result={'operation':'native_backup','state':'complete','basename':'transport-backup-fixture'}
    run=SimpleNamespace(returncode=0,stdout=('\n'.join(json.dumps(value) for value in (request,result))).encode())
    owner._probe=Mock(return_value={'archive_verified':True,'namespace':owner.namespace,'quarantine':False,'owner_bound':False})
    with patch('doom_eap.launcher.launcher_session.os.name','nt'), patch('doom_eap.launcher.launcher_session.subprocess.run',return_value=run):
        with pytest.raises(RuntimeError,match='native owner proof'):
            owner.create_backup()
        owner._probe.return_value['owner_bound']=True
        assert owner.create_backup()['basename']==result['basename']
        request['instance_id']='d'*32
        run.stdout=('\n'.join(json.dumps(value) for value in (request,result))).encode()
        with pytest.raises(RuntimeError,match='not confirmed'):
            owner.create_backup()
