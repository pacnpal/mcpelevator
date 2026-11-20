// A reactive stand-in for `$app/state`'s `page`, for route tests.
let url = $state(new URL('http://localhost/login'));

export const page = {
	get url() {
		return url;
	},
	set url(next: URL) {
		url = next;
	}
};
