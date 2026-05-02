from qt_app import run_qt


if __name__ == "__main__":
    try:
        run_qt()
    except Exception as e:
        print(e)