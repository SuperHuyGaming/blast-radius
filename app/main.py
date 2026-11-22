# Entry point (CMD python /app/main.py): selector.py then loads from its compiled .pyc instead
# of being compiled from source on every start (about 40 ms of CPU). procmodel first: automatic
# garbage collection is off from the start (see procmodel.py).
import procmodel

procmodel.install()
import selector

selector.main()
